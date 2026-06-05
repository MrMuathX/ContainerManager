import docker
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import AsyncGenerator, Callable, Optional
from docker.models.containers import Container
from docker.errors import DockerException, NotFound, APIError
import io
import zipfile
import tarfile
import tempfile
import yaml  # will check if available, else use custom dump

from app.models import (
    ContainerSummary, ContainerDetail, ContainerStats,
    PortBinding, ActionResponse
)


_client_instance: Optional[docker.DockerClient] = None

def _get_client() -> docker.DockerClient:
    global _client_instance
    if _client_instance is None:
        try:
            _client_instance = docker.from_env()
        except Exception as e:
            print(f"CRITICAL: Failed to initialize Docker client: {e}")
            raise e
    return _client_instance

def resolve_port_conflicts(client: docker.DockerClient, requested_ports: dict) -> dict:
    used_ports = set()
    for c in client.containers.list(all=True):
        if c.status == "running":
            for bindings in (c.attrs.get("HostConfig", {}).get("PortBindings") or {}).values():
                if bindings:
                    for b in bindings:
                        if b.get("HostPort"):
                            try:
                                used_ports.add(int(b["HostPort"]))
                            except ValueError:
                                pass
                            
    resolved_ports = {}
    for container_port, bindings in requested_ports.items():
        if not bindings:
            continue
        new_bindings = []
        for b in bindings:
            hp = int(b[1]) if b[1] else None
            if hp is not None:
                while hp in used_ports:
                    hp += 1
                used_ports.add(hp)
                new_bindings.append((b[0], str(hp)))
            else:
                new_bindings.append((b[0], b[1]))
        resolved_ports[container_port] = new_bindings
    return resolved_ports

def _parse_ports(container: Container) -> list[PortBinding]:
    ports = []
    # In containers.list(), ports look like:
    # [{'PublicPort': 8080, 'PrivatePort': 8080, 'Type': 'tcp', 'IP': '0.0.0.0'}]
    # In containers.get(), they look like:
    # {'8080/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8080'}]}
    
    port_attr = container.attrs.get("Ports") or container.ports or {}
    
    if isinstance(port_attr, list):
        # Format from .list()
        for p in port_attr:
            if "PublicPort" in p:
                ports.append(PortBinding(
                    host_ip=p.get("IP", ""),
                    host_port=str(p.get("PublicPort")),
                    container_port=f"{p.get('PrivatePort', '')}/{p.get('Type', 'tcp')}"
                ))
            else:
                ports.append(PortBinding(
                    host_ip="",
                    host_port="",
                    container_port=f"{p.get('PrivatePort', '')}/{p.get('Type', 'tcp')}"
                ))
        return ports
        
    for container_port, bindings in port_attr.items():
        if bindings:
            for b in bindings:
                ports.append(PortBinding(
                    host_ip=b.get("HostIp", ""),
                    host_port=b.get("HostPort", ""),
                    container_port=container_port,
                ))
        else:
            ports.append(PortBinding(
                host_ip="",
                host_port="",
                container_port=container_port,
            ))
    return ports


def _uptime(created_str: str, status: str) -> Optional[str]:
    if "running" not in status.lower():
        return None
    try:
        # Docker returns ISO 8601 with nanoseconds
        created_str = created_str[:26] + "Z"
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        total_seconds = int(delta.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        if days:
            return f"{days}d {hours}h {minutes}m"
        elif hours:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m {seconds}s"
    except Exception:
        return None


def _parse_stats(raw: dict) -> ContainerStats:
    cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - \
                raw["precpu_stats"]["cpu_usage"]["total_usage"]
    system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - \
                   raw["precpu_stats"].get("system_cpu_usage", 0)
    cpu_count = raw["cpu_stats"].get("online_cpus") or \
                len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_percent = (cpu_delta / system_delta * cpu_count * 100.0) if system_delta > 0 else 0.0

    mem_usage = raw["memory_stats"].get("usage", 0)
    mem_cache = raw["memory_stats"].get("stats", {}).get("cache", 0)
    mem_net = mem_usage - mem_cache
    mem_limit = raw["memory_stats"].get("limit", 1)

    net_rx = net_tx = 0.0
    for iface in raw.get("networks", {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    blk_read = blk_write = 0.0
    for entry in raw.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []:
        if entry.get("op") == "Read":
            blk_read += entry.get("value", 0)
        elif entry.get("op") == "Write":
            blk_write += entry.get("value", 0)

    return ContainerStats(
        cpu_percent=round(cpu_percent, 2),
        mem_usage_mb=round(mem_net / 1024 / 1024, 2),
        mem_limit_mb=round(mem_limit / 1024 / 1024, 2),
        mem_percent=round(mem_net / mem_limit * 100.0, 2) if mem_limit > 0 else 0.0,
        net_rx_mb=round(net_rx / 1024 / 1024, 2),
        net_tx_mb=round(net_tx / 1024 / 1024, 2),
        block_read_mb=round(blk_read / 1024 / 1024, 2),
        block_write_mb=round(blk_write / 1024 / 1024, 2),
    )


# ── Public API ──────────────────────────────────────────────────────────────

def list_containers(all_containers: bool = True) -> list[ContainerSummary]:
    client = _get_client()
    containers = client.containers.list(all=all_containers)
    result = []
    for c in containers:
        created_str = c.attrs.get("Created", "")
        # The list API often returns an epoch timestamp instead of an ISO string in "Created" depending on the docker version/call, wait list returns int.
        # But let's handle ISO format returning. Wait, if it's an int epoch:
        if isinstance(created_str, int):
            created_str = datetime.fromtimestamp(created_str, tz=timezone.utc).isoformat()
            
        result.append(ContainerSummary(
            id=c.id,
            short_id=c.short_id,
            name=c.name.lstrip("/") if c.name else "",
            image=c.image.tags[0] if (c.image and hasattr(c.image, 'tags') and c.image.tags) else (c.attrs.get("Image", "")),
            image_id=c.attrs.get("ImageID", ""),
            status=c.status,
            state="running" if c.status == "running" else "stopped",
            ports=_parse_ports(c),
            created=created_str[:19],
            uptime=_uptime(created_str, c.status),
            labels=c.attrs.get("Config", {}).get("Labels", {}),
            exit_code=c.attrs.get("State", {}).get("ExitCode", 0),
        ))
    return result


def get_container_detail(container_id: str) -> ContainerDetail:
    client = _get_client()
    c: Container = client.containers.get(container_id)
    c.reload()

def get_container_detail(container_id: str) -> ContainerDetail:
    client = _get_client()
    try:
        c: Container = client.containers.get(container_id)
        c.reload()
    except Exception as e:
        print(f"ERROR: Could not get/reload container {container_id}: {e}")
        raise e

    # Collect one stats snapshot (stream=False)
    stats_data = None
    if c.status == "running":
        try:
            # stats(stream=False) can hang or fail if daemon is busy
            raw = c.stats(stream=False)
            if raw:
                stats_data = _parse_stats(raw)
        except Exception as e:
            print(f"WARN: Could not fetch stats for {container_id}: {e}")
            pass

    try:
        attrs = c.attrs
        networks = list(attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
        restart_policy = attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no")
        config = attrs.get("Config", {})
        cmd = config.get("Cmd")
        command = " ".join(cmd) if isinstance(cmd, list) else (cmd or "")

        image_tags = getattr(c.image, 'tags', []) if c.image else []
        image_name = image_tags[0] if image_tags else (attrs.get("Image", "") or (c.image.short_id if c.image else "unknown"))
        image_id = getattr(c.image, 'short_id', "unknown") if c.image else "unknown"

        created_str = attrs.get("Created", "")
        if isinstance(created_str, int):
            created_str = datetime.fromtimestamp(created_str, tz=timezone.utc).isoformat()
        
        created_display = created_str[:19] if created_str else "unknown"

        return ContainerDetail(
            id=c.id,
            short_id=c.short_id,
            name=c.name.lstrip("/"),
            image=image_name,
            image_id=image_id,
            status=c.status,
            state="running" if c.status == "running" else "stopped",
            ports=_parse_ports(c),
            created=created_display,
            uptime=_uptime(created_str, c.status) if created_str else None,
            env=config.get("Env") or [],
            labels=config.get("Labels") or {},
            mounts=[
                {"type": m.get("Type"), "source": m.get("Source"), "destination": m.get("Destination"), "mode": m.get("Mode")}
                for m in (attrs.get("Mounts") or [])
            ],
            network_mode=attrs.get("HostConfig", {}).get("NetworkMode", ""),
            networks=networks,
            restart_policy=restart_policy,
            command=command,
            stats=stats_data,
        )
    except Exception as e:
        print(f"ERROR: Failed to parse detail for {container_id}: {e}")
        import traceback
        traceback.print_exc()
        raise e


def create_container(image: str, name: Optional[str], env: list[str], ports: dict) -> ActionResponse:
    try:
        client = _get_client()
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            client.images.pull(image)
        
        resolved_ports = resolve_port_conflicts(client, ports) if ports else {}
        
        c = client.containers.run(
            image,
            name=name,
            detach=True,
            environment=env,
            ports=resolved_ports
        )
        return ActionResponse(success=True, message=f"Container {c.name} created and started.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


def start_container(container_id: str) -> ActionResponse:
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        c.start()
        return ActionResponse(success=True, message=f"Container {c.name} started.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


def stop_container(container_id: str) -> ActionResponse:
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        c.stop(timeout=10)
        return ActionResponse(success=True, message=f"Container {c.name} stopped.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


def restart_container(container_id: str) -> ActionResponse:
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        c.restart(timeout=10)
        return ActionResponse(success=True, message=f"Container {c.name} restarted.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


def remove_container(container_id: str, force: bool = True) -> ActionResponse:
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        name = c.name
        c.remove(force=force)
        return ActionResponse(success=True, message=f"Container {name} removed.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))

def rename_container(container_id: str, new_name: str) -> ActionResponse:
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        old_name = c.name
        c.rename(new_name)
        return ActionResponse(success=True, message=f"Container '{old_name}' renamed to '{new_name}'.")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


def get_logs(container_id: str, tail: int = 200) -> str:
    client = _get_client()
    c = client.containers.get(container_id)
    raw = c.logs(tail=tail, timestamps=True)
    return raw.decode("utf-8", errors="replace")


async def stream_logs(container_id: str) -> AsyncGenerator[str, None]:
    """Async generator that yields log lines using the docker client."""
    client = _get_client()
    try:
        container = client.containers.get(container_id)
        # Using stream=True returns a generator for live logs
        # We wrap it in run_in_executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        
        def get_log_stream():
            return container.logs(stream=True, follow=True, tail=50, timestamps=True)
            
        log_stream = await loop.run_in_executor(None, get_log_stream)
        
        try:
            while True:
                # Read the next line from the blocking generator in an executor
                line = await loop.run_in_executor(None, next, log_stream, None)
                if line is None:
                    break
                if isinstance(line, bytes):
                    yield line.decode('utf-8', errors='replace').rstrip()
                else:
                    yield str(line).rstrip()
        finally:
            if hasattr(log_stream, 'close'):
                log_stream.close()
                
    except asyncio.CancelledError:
        pass
    except Exception as e:
        yield f"[Error streaming logs] {str(e)}"

async def update_container(container_id: str):
    """
    Async generator yielding JSON progress lines.
    Pulls the latest image, then recreates the container.
    """
    client = _get_client()
    c = client.containers.get(container_id)
    image_tag = c.image.tags[0] if c.image.tags else None

    if not image_tag:
        yield json.dumps({"status": "error", "message": "Container has no image tag; cannot pull."})
        return

    # Capture container config for recreation
    attrs = c.attrs
    host_config = attrs.get("HostConfig", {})
    config = attrs.get("Config", {})
    name = c.name.lstrip("/")

    yield json.dumps({"status": "pulling", "message": f"Pulling {image_tag}..."})

    loop = asyncio.get_event_loop()
    try:
        for line in client.api.pull(image_tag, stream=True, decode=True):
            status = line.get("status", "")
            progress = line.get("progress", "")
            layer = line.get("id", "")
            yield json.dumps({"status": "progress", "layer": layer, "message": f"{status} {progress}".strip()})
            await asyncio.sleep(0)
    except Exception as e:
        yield json.dumps({"status": "error", "message": f"Pull failed: {e}"})
        return

    yield json.dumps({"status": "recreating", "message": "Stopping old container..."})
    try:
        c.stop(timeout=10)
        c.remove(force=True)
    except Exception as e:
        yield json.dumps({"status": "error", "message": f"Remove failed: {e}"})
        return

    yield json.dumps({"status": "recreating", "message": "Starting new container..."})
    try:
        # Rebuild port bindings
        port_bindings = host_config.get("PortBindings") or {}
        exposed_ports = config.get("ExposedPorts") or {}
        volumes = host_config.get("Binds") or []
        env = config.get("Env") or []
        labels = config.get("Labels") or {}
        restart_policy = host_config.get("RestartPolicy") or {"Name": "no"}
        network_mode = host_config.get("NetworkMode", "bridge")

        # Port conflict resolution
        raw_ports = {k: [(b["HostIp"], b["HostPort"]) for b in v] for k, v in port_bindings.items() if v} if port_bindings else {}
        resolved_ports = resolve_port_conflicts(client, raw_ports) if raw_ports else {}

        new_container = client.containers.run(
            image_tag,
            name=name,
            detach=True,
            environment=env,
            labels=labels,
            ports=resolved_ports,
            volumes=volumes,
            restart_policy=restart_policy,
            network_mode=network_mode,
        )
        yield json.dumps({"status": "done", "message": f"Container {name} updated and running.", "id": new_container.short_id})
    except Exception as e:
        yield json.dumps({"status": "error", "message": f"Recreation failed: {e}"})

def export_container_image(container_id: str):
    """Returns a generator yielding the container's image as a tar archive."""
    client = _get_client()
    c = client.containers.get(container_id)
    image = c.image
    if not image:
        raise ValueError("Container has no associated image.")
    return image.save()


def _ensure_helper_image(client: docker.DockerClient) -> None:
    """Ensure helper image exists for volume backup/restore."""
    try:
        client.images.get("busybox:latest")
    except docker.errors.ImageNotFound:
        client.images.pull("busybox:latest")


def _write_stream_to_file(stream, target_path: str) -> None:
    with open(target_path, "wb") as f:
        for chunk in stream:
            if chunk:
                f.write(chunk)


def _backup_named_volume(client: docker.DockerClient, volume_name: str, target_path: str) -> None:
    _ensure_helper_image(client)
    helper = client.containers.create(
        "busybox:latest",
        command=["sh", "-c", "sleep 300"],
        volumes={volume_name: {"bind": "/volume", "mode": "ro"}},
    )
    try:
        helper.start()
        exec_id = client.api.exec_create(
            helper.id,
            cmd=["tar", "-cf", "-", "-C", "/volume", "."],
        )["Id"]
        stream = client.api.exec_start(exec_id, stream=True)
        _write_stream_to_file(stream, target_path)
        inspect = client.api.exec_inspect(exec_id)
        if inspect.get("ExitCode", 1) != 0:
            raise RuntimeError(f"Failed to backup volume '{volume_name}'.")
    finally:
        try:
            helper.remove(force=True)
        except Exception:
            pass


def _restore_named_volume(client: docker.DockerClient, volume_name: str, backup_tar_path: str) -> None:
    _ensure_helper_image(client)
    try:
        client.volumes.get(volume_name)
    except docker.errors.NotFound:
        client.volumes.create(name=volume_name)

    helper = client.containers.create(
        "busybox:latest",
        command=["sh", "-c", "sleep 300"],
        volumes={volume_name: {"bind": "/volume", "mode": "rw"}},
    )
    try:
        helper.start()
        with open(backup_tar_path, "rb") as f:
            ok = helper.put_archive("/volume", f.read())
        if not ok:
            raise RuntimeError(f"Failed to restore volume '{volume_name}'.")
    finally:
        try:
            helper.remove(force=True)
        except Exception:
            pass


def _sanitize_image_name_for_tag(image_name: str) -> str:
    safe = image_name.replace("/", "_").replace(":", "_").replace("@", "_")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in safe).strip("._-") or "container"


def _import_filesystem_tar_as_image(
    client: docker.DockerClient,
    tar_path: str,
    container_name: str,
) -> str:
    """Import a docker export tar via the low-level API (client.api, not client.images)."""
    tagged_repo = f"cm-import/{_sanitize_image_name_for_tag(container_name)}"
    tagged_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    with open(tar_path, "rb") as f:
        client.api.import_image_from_data(
            f.read(),
            repository=tagged_repo,
            tag=tagged_tag,
        )
    return f"{tagged_repo}:{tagged_tag}"


def _container_backup_manifest(c: Container) -> dict:
    attrs = c.attrs
    config = attrs.get("Config", {})
    host_config = attrs.get("HostConfig", {})
    state = attrs.get("State", {})
    mounts = attrs.get("Mounts", []) or []

    image_name = config.get("Image")
    if not image_name and c.image:
        image_tags = getattr(c.image, "tags", []) or []
        image_name = image_tags[0] if image_tags else getattr(c.image, "id", None)

    return {
        "format_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "container_id": c.id,
        "container_name": c.name.lstrip("/"),
        "image_name": image_name,
        "state": {"status": state.get("Status", c.status), "was_running": c.status == "running"},
        "config": {
            "env": config.get("Env") or [],
            "labels": config.get("Labels") or {},
            "cmd": config.get("Cmd"),
            "entrypoint": config.get("Entrypoint"),
            "working_dir": config.get("WorkingDir"),
            "user": config.get("User"),
            "restart_policy": host_config.get("RestartPolicy") or {"Name": "no"},
            "network_mode": host_config.get("NetworkMode", "bridge"),
            "port_bindings": host_config.get("PortBindings") or {},
            "mounts": [
                {
                    "type": m.get("Type"),
                    "name": m.get("Name"),
                    "source": m.get("Source"),
                    "destination": m.get("Destination"),
                    "rw": m.get("RW", True),
                }
                for m in mounts
            ],
        },
    }

def get_container_compose(container_id: str) -> str:
    """Reconstruct a docker-compose.yaml equivalent from container inspect data."""
    client = _get_client()
    c = client.containers.get(container_id)
    attrs = c.attrs
    
    config = attrs.get("Config", {})
    host_config = attrs.get("HostConfig", {})
    
    # 1. Base info
    service = {
        "image": config.get("Image", ""),
        "container_name": c.name.lstrip("/"),
        "restart": host_config.get("RestartPolicy", {}).get("Name", "no")
    }
    
    # 2. Environment
    env = config.get("Env")
    if env:
        service["environment"] = env
        
    # 3. Ports
    port_bindings = host_config.get("PortBindings")
    if port_bindings:
        ports = []
        for container_port, bindings in port_bindings.items():
            if bindings:
                for b in bindings:
                    hp = b.get("HostPort")
                    if hp:
                        ports.append(f"{hp}:{container_port}")
        if ports:
            service["ports"] = ports
            
    # 4. Volumes
    mounts = attrs.get("Mounts")
    if mounts:
        volumes = []
        for m in mounts:
            source = m.get("Source")
            dest = m.get("Destination")
            if source and dest:
                volumes.append(f"{source}:{dest}")
        if volumes:
            service["volumes"] = volumes
            
    # 5. Networks
    nets = attrs.get("NetworkSettings", {}).get("Networks", {})
    if nets:
        service["networks"] = list(nets.keys())

    # Build simple YAML string manually to avoid dependency issues or messy formatting
    lines = ["version: '3.8'", "", "services:", f"  {service['container_name']}:"]
    for k, v in service.items():
        if isinstance(v, list):
            lines.append(f"    {k}:")
            for item in v:
                lines.append(f"      - {item}")
        elif isinstance(v, dict):
            lines.append(f"    {k}:")
            for subk, subv in v.items():
                lines.append(f"      {subk}: {subv}")
        else:
            lines.append(f"    {k}: {v}")
            
    return "\n".join(lines)


def _pack_backup_zip(source_dir: str, zip_path: str) -> None:
    """Pack backup workspace into a ZIP. Large tar blobs use STORED (already compressed)."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(source_dir):
            for file_name in files:
                abs_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(abs_path, source_dir)
                compress = zipfile.ZIP_STORED if file_name.endswith(".tar") else zipfile.ZIP_DEFLATED
                z.write(abs_path, rel_path, compress_type=compress)


def _build_container_backup_workspace(
    container_id: str,
    tmpdir: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> tuple[str, list[str]]:
    """
    Build backup artifacts in tmpdir:
    - container config/metadata
    - writable container filesystem tar (docker export)
    - named volume snapshots

    image.tar is omitted; import restores from filesystem.tar (faster, smaller backups).
    """
    def progress(pct: int, message: str) -> None:
        if on_progress:
            on_progress(pct, message)

    client = _get_client()
    progress(5, "Reading container metadata")
    c = client.containers.get(container_id)
    name = c.name.lstrip("/")

    manifest = _container_backup_manifest(c)
    mounts = manifest.get("config", {}).get("mounts", [])
    named_volumes = [m for m in mounts if m.get("type") == "volume" and m.get("name")]
    warnings = []

    meta_dir = os.path.join(tmpdir, "meta")
    volumes_dir = os.path.join(tmpdir, "volumes")
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(volumes_dir, exist_ok=True)

    with open(os.path.join(meta_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    with open(os.path.join(meta_dir, "docker-compose.yaml"), "w", encoding="utf-8") as f:
        f.write(get_container_compose(container_id))

    progress(10, f"Exporting filesystem for {name}")
    filesystem_tar_path = os.path.join(tmpdir, "filesystem.tar")
    _write_stream_to_file(c.export(), filesystem_tar_path)
    progress(55, "Filesystem export complete")

    vol_count = len(named_volumes)
    for idx, v in enumerate(named_volumes):
        v_name = v["name"]
        if vol_count:
            pct = 55 + int((idx + 1) / vol_count * 30)
        else:
            pct = 85
        progress(pct, f"Backing up volume {v_name}")
        v_tar_path = os.path.join(volumes_dir, f"{v_name}.tar")
        try:
            _backup_named_volume(client, v_name, v_tar_path)
        except Exception as e:
            warnings.append(f"Volume backup failed for '{v_name}': {e}")
            progress(pct, f"Volume backup failed for '{v_name}': {e}")

    info_lines = [
        f"Backup for container: {name}",
        f"Generated: {datetime.now().isoformat()}",
        "",
        "Included artifacts:",
        "- filesystem.tar (docker export output)",
        "- meta/manifest.json",
        "- meta/docker-compose.yaml",
        "- image.tar omitted (restore uses filesystem export)",
    ]
    if named_volumes:
        info_lines.append("- volumes/*.tar (named volume snapshots)")
    else:
        info_lines.append("- volumes: none (or only bind mounts)")
    if warnings:
        info_lines.append("")
        info_lines.append("Warnings:")
        info_lines.extend([f"- {w}" for w in warnings])
    with open(os.path.join(meta_dir, "backup_info.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(info_lines))

    return name, warnings


def create_container_backup_file(
    container_id: str,
    zip_path: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> str:
    """Write a full container backup ZIP to disk. Returns the container name."""
    def progress(pct: int, message: str) -> None:
        if on_progress:
            on_progress(pct, message)

    with tempfile.TemporaryDirectory(prefix="cm-backup-") as tmpdir:
        name, _ = _build_container_backup_workspace(container_id, tmpdir, on_progress=on_progress)
        progress(90, "Packing backup ZIP")
        _pack_backup_zip(tmpdir, zip_path)
        progress(100, "Backup complete")
    return name


def create_container_backup(container_id: str) -> io.BytesIO:
    """Create a full portable backup ZIP in memory (prefer create_container_backup_file for large containers)."""
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="cm-backup-")
    os.close(fd)
    try:
        create_container_backup_file(container_id, path)
        with open(path, "rb") as f:
            buffer = io.BytesIO(f.read())
        buffer.seek(0)
        return buffer
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def import_container_backup(backup_bytes: bytes, requested_name: Optional[str] = None) -> ActionResponse:
    """Restores a container from a backup ZIP (sync wrapper)."""
    result_message = ""
    success = False
    for line in import_container_backup_stream(backup_bytes, requested_name):
        data = json.loads(line)
        if data.get("status") == "done":
            success = True
            result_message = data.get("message", "Import complete.")
        elif data.get("status") == "error":
            return ActionResponse(success=False, message=data.get("message", "Import failed."))
    if success:
        return ActionResponse(success=True, message=result_message)
    return ActionResponse(success=False, message="Import failed.")


def import_container_backup_stream(backup_bytes: bytes, requested_name: Optional[str] = None):
    """Yields NDJSON progress lines while restoring a container backup."""
    client = _get_client()

    def emit(status: str, progress: int, message: str, **extra) -> str:
        return json.dumps({"status": status, "progress": progress, "message": message, **extra})

    try:
        yield emit("progress", 5, "Extracting backup archive")
        with tempfile.TemporaryDirectory(prefix="cm-import-") as tmpdir:
            backup_zip_path = os.path.join(tmpdir, "backup.zip")
            with open(backup_zip_path, "wb") as f:
                f.write(backup_bytes)

            with zipfile.ZipFile(backup_zip_path, "r") as z:
                z.extractall(tmpdir)

            yield emit("progress", 10, "Reading manifest")
            manifest_path = os.path.join(tmpdir, "meta", "manifest.json")
            if not os.path.exists(manifest_path):
                yield emit("error", 0, "Invalid backup: missing meta/manifest.json")
                return

            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            cfg = manifest.get("config", {})
            container_name = requested_name or manifest.get("container_name") or f"imported-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            was_running = bool(manifest.get("state", {}).get("was_running", True))

            try:
                client.containers.get(container_name)
                container_name = f"{container_name}-imported-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                yield emit("progress", 12, f"Name in use; using {container_name}")
            except docker.errors.NotFound:
                pass

            image_tar_path = os.path.join(tmpdir, "image.tar")
            if os.path.exists(image_tar_path):
                yield emit("progress", 20, "Loading image from backup")
                with open(image_tar_path, "rb") as f:
                    client.images.load(f.read())

            filesystem_tar_path = os.path.join(tmpdir, "filesystem.tar")
            image_name = manifest.get("image_name")
            if os.path.exists(filesystem_tar_path):
                yield emit("progress", 30, "Importing container filesystem")
                image_name = _import_filesystem_tar_as_image(client, filesystem_tar_path, container_name)
                yield emit("progress", 45, f"Tagged imported image as {image_name}")

            if not image_name:
                yield emit("error", 0, "Backup does not contain a valid image reference.")
                return

            mounts = cfg.get("mounts", [])
            bind_specs = []
            volume_mounts = [
                m for m in mounts
                if m.get("type") == "volume" and m.get("name") and m.get("destination")
            ]
            volumes_restored = 0
            for m in mounts:
                m_type = m.get("type")
                dest = m.get("destination")
                rw = m.get("rw", True)
                if m_type == "volume" and m.get("name") and dest:
                    v_name = m["name"]
                    v_tar_path = os.path.join(tmpdir, "volumes", f"{v_name}.tar")
                    if os.path.exists(v_tar_path):
                        volumes_restored += 1
                        if volume_mounts:
                            pct = 45 + int(volumes_restored / len(volume_mounts) * 35)
                        else:
                            pct = 80
                        yield emit("progress", pct, f"Restoring volume {v_name}")
                        _restore_named_volume(client, v_name, v_tar_path)
                    mode = "rw" if rw else "ro"
                    bind_specs.append(f"{v_name}:{dest}:{mode}")
                elif m_type == "bind" and m.get("source") and dest:
                    mode = "rw" if rw else "ro"
                    bind_specs.append(f"{m['source']}:{dest}:{mode}")

            yield emit("progress", 85, "Resolving port bindings")
            raw_port_bindings = cfg.get("port_bindings") or {}
            requested_ports = {}
            for container_port, bindings in raw_port_bindings.items():
                if not bindings:
                    continue
                requested_ports[container_port] = [
                    (b.get("HostIp", ""), b.get("HostPort", ""))
                    for b in bindings
                ]
            resolved_ports = resolve_port_conflicts(client, requested_ports) if requested_ports else {}

            yield emit("progress", 90, f"Creating container {container_name}")
            new_container = client.containers.run(
                image_name,
                name=container_name,
                detach=True,
                environment=cfg.get("env") or [],
                labels=cfg.get("labels") or {},
                command=cfg.get("cmd"),
                entrypoint=cfg.get("entrypoint"),
                working_dir=cfg.get("working_dir") or None,
                user=cfg.get("user") or None,
                restart_policy=cfg.get("restart_policy") or {"Name": "no"},
                network_mode=cfg.get("network_mode") or "bridge",
                ports=resolved_ports,
                volumes=bind_specs or None,
            )

            if not was_running:
                yield emit("progress", 95, "Stopping container (was stopped in backup)")
                new_container.stop(timeout=5)

            message = f"Backup imported as container '{container_name}' ({new_container.short_id})."
            yield emit("done", 100, message, container_id=new_container.id, container_name=container_name)
    except Exception as e:
        yield emit("error", 0, f"Import failed: {e}")
