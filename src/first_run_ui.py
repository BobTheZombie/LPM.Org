from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, TextIO

from lpm import config


@dataclass(frozen=True)
class ConfigField:
    """Description of an interactive configuration field."""

    key: str
    prompt: str
    parser: Callable[[str], object]
    default: object
    help_text: Optional[str] = None
    optional: bool = False
    formatter: Callable[[object], str] = lambda value: "" if value is None else str(value)


_TRUE_SET = {"y", "yes", "true", "1", "on"}
_FALSE_SET = {"n", "no", "false", "0", "off"}
_CPU_CHOICES = {"x86_64v1", "x86_64v2", "x86_64v3", "x86_64v4"}
_SANDBOX_CHOICES = ("none", "fakeroot", "bwrap")
_OPT_LEVELS = ("-Os", "-O2", "-O3", "-Ofast")
_DEFAULT_LPMBUILD_REPO = "https://gitlab.com/lpm-org/packages/-/raw/main/"
_DEFAULT_BINARY_REPO = (
    "https://gitlab.com/lpm-org/lpm-org-official-binaries/{name}/"
    "{name}-{version}-{release}.{arch}.lpm"
)


def _identity(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be empty")
    return cleaned


def _parse_choice(options: Iterable[str]) -> Callable[[str], str]:
    normalized = {opt.lower(): opt for opt in options}

    def _parser(value: str) -> str:
        choice = value.strip().lower()
        if choice not in normalized:
            raise ValueError(f"choose one of: {', '.join(options)}")
        return normalized[choice]

    return _parser


def _parse_yes_no(value: str) -> bool:
    choice = value.strip().lower()
    if choice in _TRUE_SET:
        return True
    if choice in _FALSE_SET:
        return False
    raise ValueError("enter yes or no")


def _parse_install_default(value: str) -> str:
    choice = value.strip().lower()
    if choice in {"y", "yes"}:
        return "y"
    if choice in {"n", "no"}:
        return "n"
    raise ValueError("enter y or n")


def _parse_cpu_type(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "").replace("_", "")
    if not cleaned:
        raise ValueError("value cannot be empty")
    key = f"x86_64v{cleaned[-1]}" if cleaned.startswith("x8664v") and cleaned[-1] in "1234" else value.strip()
    if key.lower() not in _CPU_CHOICES:
        raise ValueError("valid values: x86_64v1, x86_64v2, x86_64v3, x86_64v4")
    return key.replace("X86", "x86").replace("V", "v")


def _parse_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be empty")
    return str(Path(cleaned).expanduser())


def _parse_optional_string(value: str) -> str:
    return value.strip()


def _parse_optional_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    return str(Path(cleaned).expanduser())


def _parse_non_negative_int(value: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be empty")
    try:
        parsed = int(cleaned, 10)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError("enter a non-negative integer") from exc
    if parsed < 0:
        raise ValueError("enter a non-negative integer")
    return parsed


def _parse_positive_int(value: str) -> int:
    parsed = _parse_non_negative_int(value)
    if parsed <= 0:
        raise ValueError("enter a positive integer")
    return parsed


def _parse_probability(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be empty")
    try:
        parsed = float(cleaned)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError("enter a decimal value between 0 and 1") from exc
    if not (0 < parsed <= 1):
        raise ValueError("enter a decimal value between 0 and 1")
    return str(parsed)


def _parse_opt_level(value: str) -> str:
    cleaned = value.strip()
    if cleaned not in _OPT_LEVELS:
        raise ValueError("choose one of: -Os, -O2, -O3, -Ofast")
    return cleaned


def _parse_yes_no_str(value: str) -> str:
    return "yes" if _parse_yes_no(value) else "no"


def _format_yes_no_str(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_SET:
            return "yes"
        if lowered in _FALSE_SET:
            return "no"
    return str(value)


def _default_yes_no(key: str, fallback_true: bool) -> str:
    raw = config.CONF.get(key)
    if raw is None:
        return "yes" if fallback_true else "no"
    lowered = str(raw).strip().lower()
    if lowered in _TRUE_SET:
        return "yes"
    if lowered in _FALSE_SET:
        return "no"
    return "yes" if fallback_true else "no"


def _build_fields() -> tuple[tuple[ConfigField, ...], tuple[ConfigField, ...]]:
    base_fields = (
        ConfigField(
            key="ARCH",
            prompt="System architecture",
            parser=_identity,
            default=config.ARCH or "x86_64",
            help_text="Reported by `uname -m`.",
        ),
        ConfigField(
            key="INIT_POLICY",
            prompt="Init system integration policy (auto/manual/none)",
            parser=_parse_choice(["auto", "manual", "none"]),
            default=config.CONF.get("INIT_POLICY", "auto"),
            help_text="Use 'auto' to enable detected services after installs.",
        ),
        ConfigField(
            key="SANDBOX_MODE",
            prompt="Preferred build sandbox (none/fakeroot/bwrap)",
            parser=_parse_choice(_SANDBOX_CHOICES),
            default=(
                config.CONF.get("SANDBOX_MODE", "none") or "none"
            ).strip().lower()
            or "none",
            help_text="Choose 'none' to run builds without a sandbox.",
        ),
        ConfigField(
            key="OPT_LEVEL",
            prompt="Default compiler optimisation level (-Os/-O2/-O3/-Ofast)",
            parser=_parse_opt_level,
            default=config.OPT_LEVEL,
        ),
        ConfigField(
            key="FETCH_MAX_WORKERS",
            prompt="Maximum concurrent download workers",
            parser=_parse_positive_int,
            default=config.FETCH_MAX_WORKERS,
        ),
        ConfigField(
            key="IO_BUFFER_SIZE",
            prompt="IO buffer size in bytes (minimum 65536)",
            parser=_parse_positive_int,
            default=config.IO_BUFFER_SIZE,
        ),
        ConfigField(
            key="STATE_DIR",
            prompt="State directory for cached data",
            parser=_parse_path,
            default=str(config.STATE_DIR),
        ),
        ConfigField(
            key="MAX_SNAPSHOTS",
            prompt="Maximum filesystem snapshots to retain",
            parser=_parse_non_negative_int,
            default=config.MAX_SNAPSHOTS,
        ),
        ConfigField(
            key="MAX_LEARNT_CLAUSES",
            prompt="Maximum learnt clauses for dependency solver",
            parser=_parse_positive_int,
            default=config.MAX_LEARNT_CLAUSES,
        ),
        ConfigField(
            key="VSIDS_VAR_DECAY",
            prompt="VSIDS variable activity decay (0-1)",
            parser=_parse_probability,
            default=config.CONF.get("VSIDS_VAR_DECAY", "0.95"),
        ),
        ConfigField(
            key="VSIDS_CLAUSE_DECAY",
            prompt="VSIDS clause activity decay (0-1)",
            parser=_parse_probability,
            default=config.CONF.get("VSIDS_CLAUSE_DECAY", "0.999"),
        ),
        ConfigField(
            key="BUILDPKG_WORKERS",
            prompt="Maximum concurrent lpmbuild workers",
            parser=_parse_positive_int,
            default=(
                config.CONF.get("BUILDPKG_WORKERS")
                or max(2, min(8, os.cpu_count() or 1))
            ),
        ),
        ConfigField(
            key="INSTALL_PROMPT_DEFAULT",
            prompt="Default answer for install prompts (y/n)",
            parser=_parse_install_default,
            default=(config.INSTALL_PROMPT_DEFAULT or "n"),
        ),
        ConfigField(
            key="ALLOW_LPMBUILD_FALLBACK",
            prompt="Enable GitLab fallback downloads? (yes/no)",
            parser=_parse_yes_no,
            default=config.ALLOW_LPMBUILD_FALLBACK,
            formatter=lambda value: "yes" if value else "no",
        ),
        ConfigField(
            key="ENABLE_CPU_OPTIMIZATIONS",
            prompt=(
                "Enable automatic optimisation using -march="
                f"{config.MARCH} / -mtune={config.MTUNE}? (yes/no)"
            ),
            parser=_parse_yes_no,
            default=config.ENABLE_CPU_OPTIMIZATIONS,
            formatter=lambda value: "yes" if value else "no",
            help_text=(
                "Set to 'no' to disable CPU-specific -march/-mtune flags and rely "
                "on package-defined settings or a manual CPU_TYPE override."
            ),
        ),
        ConfigField(
            key="CPU_TYPE",
            prompt="Override CPU type (x86_64v1-4 or blank)",
            parser=_parse_cpu_type,
            default=config.CONF.get("CPU_TYPE", ""),
            help_text="Leave empty to keep auto-detected CPU tuning.",
            optional=True,
        ),
        ConfigField(
            key="LPMBUILD_REPO",
            prompt="lpmbuild source repository URL template",
            parser=_identity,
            default=config.CONF.get("LPMBUILD_REPO", _DEFAULT_LPMBUILD_REPO),
        ),
        ConfigField(
            key="BINARY_REPO",
            prompt="Binary package repository URL template",
            parser=_identity,
            default=config.CONF.get("BINARY_REPO", _DEFAULT_BINARY_REPO),
        ),
        ConfigField(
            key="COPY_OUT_DIR",
            prompt="Directory to copy built artifacts (blank to disable)",
            parser=_parse_optional_path,
            default=config.CONF.get("COPY_OUT_DIR", ""),
            optional=True,
        ),
        ConfigField(
            key="ALWAYS_SIGN",
            prompt="Always sign builds when key is available? (yes/no)",
            parser=_parse_yes_no_str,
            default=_default_yes_no("ALWAYS_SIGN", True),
            formatter=_format_yes_no_str,
        ),
        ConfigField(
            key="DISTRO_MAINTAINER_MODE",
            prompt="Enable distribution maintainer mode? (yes/no)",
            parser=_parse_yes_no,
            default=config.DISTRO_MAINTAINER_MODE,
            formatter=lambda value: "yes" if value else "no",
        ),
    )

    maintainer_fields = (
        ConfigField(
            key="DISTRO_NAME",
            prompt="Distribution name",
            parser=_identity,
            default=config.DISTRO_NAME or "",
            optional=True,
        ),
        ConfigField(
            key="DISTRO_REPO_ROOT",
            prompt="Local package repository directory",
            parser=_parse_path,
            default=str(config.DISTRO_REPO_ROOT),
        ),
        ConfigField(
            key="DISTRO_REPO_BASE_URL",
            prompt="Base URL for published repository (blank for local)",
            parser=_parse_optional_string,
            default=config.DISTRO_REPO_BASE_URL or "",
            optional=True,
        ),
        ConfigField(
            key="DISTRO_SOURCE_ROOT",
            prompt="Directory to archive build sources",
            parser=_parse_path,
            default=str(config.DISTRO_SOURCE_ROOT),
        ),
        ConfigField(
            key="DISTRO_LPMBUILD_ROOT",
            prompt="Directory to store lpmbuild scripts",
            parser=_parse_path,
            default=str(config.DISTRO_LPMBUILD_ROOT),
        ),
        ConfigField(
            key="DISTRO_GIT_ENABLED",
            prompt="Enable git publishing for maintainer artifacts? (yes/no)",
            parser=_parse_yes_no,
            default=config.DISTRO_GIT_ENABLED,
            formatter=lambda value: "yes" if value else "no",
        ),
        ConfigField(
            key="DISTRO_GIT_ROOT",
            prompt="Git repository root (if different from package repo)",
            parser=_parse_path,
            default=str(config.DISTRO_GIT_ROOT),
        ),
        ConfigField(
            key="DISTRO_GIT_REMOTE",
            prompt="Git remote name or URL for pushes (blank to skip)",
            parser=_parse_optional_string,
            default=config.DISTRO_GIT_REMOTE or "",
            optional=True,
        ),
        ConfigField(
            key="DISTRO_GIT_BRANCH",
            prompt="Git branch to push updates",
            parser=_identity,
            default=config.DISTRO_GIT_BRANCH or "main",
        ),
        ConfigField(
            key="DISTRO_LPMSPEC_PATH",
            prompt="Path to lpmspec manifest file",
            parser=_parse_path,
            default=str(config.DISTRO_LPMSPEC_PATH),
        ),
    )

    return base_fields, maintainer_fields


_BUILD_INFO_PATHS = (Path("/usr/share/lpm/build-info.json"),)


def _load_build_info() -> Dict[str, str]:
    candidates = []

    env_path = os.environ.get("LPM_BUILD_INFO")
    if env_path:
        candidates.append(Path(env_path))

    module_path = Path(__file__).resolve()
    candidates.append(module_path.with_name("_build_info.json"))

    parents = module_path.parents
    if len(parents) >= 2:
        project_root = parents[1]
        candidates.append(project_root / "build" / "build-info.json")
        candidates.append(project_root / "usr" / "share" / "lpm" / "build-info.json")

    try:
        exe_path = Path(sys.argv[0]).resolve()
    except Exception:
        exe_path = None
    else:
        candidates.append(exe_path.parent / "_build_info.json")
        candidates.append(exe_path.parent / ".." / "share" / "lpm" / "build-info.json")

    candidates.extend(_BUILD_INFO_PATHS)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return {
                str(key): str(value)
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, (str, int, float))
            }
    return {}


def _gather_metadata() -> Mapping[str, str]:
    metadata: Dict[str, str] = {}

    try:
        module = import_module("lpm")
    except Exception:
        module = None

    if module is not None and hasattr(module, "get_runtime_metadata"):
        try:
            module_metadata = module.get_runtime_metadata()
        except Exception:
            module_metadata = {}
        if isinstance(module_metadata, Mapping):
            metadata.update(module_metadata)  # type: ignore[arg-type]

    build_info = _load_build_info()
    if build_info:
        for key in ("version", "build", "build_date"):
            value = build_info.get(key)
            if value:
                metadata[key] = value

    return metadata


def _print_header(out: TextIO, metadata: Mapping[str, str], init_system: str, cpu_info: Mapping[str, str]) -> None:
    out.write("=" * 72 + "\n")
    out.write("LPM first-run setup\n")
    out.write("=" * 72 + "\n")
    out.write(
        "This wizard creates /etc/lpm/lpm.conf. Press Enter to accept the suggested value\n"
        "for any setting. You can re-run it later with 'lpm setup'.\n\n"
    )
    out.write("Runtime metadata:\n")
    out.write(f"  Name       : {metadata.get('name', 'unknown')}\n")
    out.write(f"  Version    : {metadata.get('version', 'unknown')}\n")
    out.write(f"  Build      : {metadata.get('build', 'development')}\n")
    build_date = metadata.get("build_date") or "unknown"
    out.write(f"  Build date : {build_date}\n")
    developer = metadata.get("developer")
    if developer:
        out.write(f"  Developer  : {developer}\n")
    project_url = metadata.get("url")
    if project_url:
        out.write(f"  Project URL: {project_url}\n")
    out.write("\n")
    out.write(f"Detected init system : {init_system}\n")
    out.write(
        "Detected CPU         : "
        f"vendor={cpu_info.get('vendor', 'unknown')} family={cpu_info.get('family', 'unknown')}\n"
    )
    out.write(
        f"Suggested tuning     : -march={cpu_info.get('march', 'generic')} -mtune={cpu_info.get('mtune', 'generic')}\n"
    )
    out.write("\n")


def _prompt_field(field: ConfigField, inp: TextIO, out: TextIO) -> Optional[object]:
    if field.help_text:
        out.write(f"{field.help_text}\n")
    display_default = field.formatter(field.default)
    prompt = f"{field.prompt} [{display_default or 'none'}]: "
    while True:
        out.write(prompt)
        out.flush()
        raw = inp.readline()
        if raw == "":
            out.write("\n")
            if field.optional and not field.default:
                return None
            return field.default
        value = raw.strip()
        if not value:
            if field.optional and not field.default:
                return None
            return field.default
        try:
            return field.parser(value)
        except ValueError as exc:  # pragma: no cover - user interaction loop
            out.write(f"Invalid value: {exc}\n")


def run_first_run_wizard(
    *,
    conf_path: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    metadata: Optional[Mapping[str, str]] = None,
    init_system: Optional[str] = None,
    cpu_info: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    """Run the interactive first-run wizard and persist configuration."""

    if conf_path is None:
        conf_path = config.CONF_FILE
    if input_stream is None:
        input_stream = sys.stdin
    if output_stream is None:
        output_stream = sys.stdout
    if metadata is None:
        metadata = _gather_metadata()
    if init_system is None:
        init_system = config.detect_init_system()
    if cpu_info is None:
        cpu_info = {
            "vendor": config.CPU_VENDOR,
            "family": config.CPU_FAMILY,
            "march": config.MARCH,
            "mtune": config.MTUNE,
        }

    _print_header(output_stream, metadata, init_system, cpu_info)

    base_fields, maintainer_fields = _build_fields()
    selections: Dict[str, object] = {}
    maintainer_enabled = config.DISTRO_MAINTAINER_MODE

    for field in base_fields:
        value = _prompt_field(field, input_stream, output_stream)
        if value is None and field.optional:
            continue
        selections[field.key] = value
        if field.key == "DISTRO_MAINTAINER_MODE":
            maintainer_enabled = bool(value)

    if maintainer_enabled:
        for field in maintainer_fields:
            value = _prompt_field(field, input_stream, output_stream)
            if value is None and field.optional:
                continue
            selections[field.key] = value

    output_stream.write("\nSaving configuration...\n")
    config.save_conf(selections, path=conf_path)
    output_stream.write(f"Configuration written to {conf_path}\n")
    output_stream.flush()
    return selections


__all__ = ["run_first_run_wizard"]
