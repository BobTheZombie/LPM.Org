"""Python distribution aliases and version-aware installed-package reuse."""
import re
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def aliases(name, provides=()):
    result = set()
    canonical = canonicalize_name(name)
    if canonical.startswith('python-'):
        result.add(canonical[7:])
    for provided in provides:
        match = re.fullmatch(r'pypi\(([^)]+)\)', provided)
        if match:
            result.add(canonicalize_name(match[1]))
    return {value for value in result if value}


def satisfied(requirement, installed):
    if isinstance(requirement, str):
        requirement = Requirement(requirement)
    if requirement.marker and not requirement.marker.evaluate():
        return True
    # Installed metadata cannot attest to optional extras or URL provenance.
    if requirement.extras or requirement.url:
        return False
    name = canonicalize_name(requirement.name)
    wanted = name[7:] if name.startswith('python-') else name
    for package_name, metadata in installed.items():
        provided = metadata.get('provides', [])
        if wanted not in aliases(package_name, provided):
            continue
        try:
            if requirement.specifier.contains(str(metadata['version']), prereleases=None):
                return True
        except (ValueError, KeyError):
            continue
    return False
