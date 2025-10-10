from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, TextIO

import src.config as config


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
            key="CPU_TYPE",
            prompt="Override CPU type (x86_64v1-4 or blank)",
            parser=_parse_cpu_type,
            default=config.CONF.get("CPU_TYPE", ""),
            help_text="Leave empty to keep auto-detected CPU tuning.",
            optional=True,
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
    )

    return base_fields, maintainer_fields


def _gather_metadata() -> Mapping[str, str]:
    module = import_module("lpm")
    return module.get_runtime_metadata()


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
