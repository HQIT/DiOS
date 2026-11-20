"""为受管常驻服务解析所选 Skill 的只读挂载源。"""

import re
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Skill

INLINE_SKILL_DIR = ".diagent-inline-skills"
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER = re.compile(r"\A---[^\S\r\n]*\r?\n(.*?)\r?\n---[^\S\r\n]*(?:\r?\n|\Z)", re.DOTALL)


def is_skill_name(value: object) -> bool:
    """目录名与 DSH catalog 使用相同的 Agent Skills 命名格式。"""
    return isinstance(value, str) and len(value) <= 64 and bool(_NAME.fullmatch(value))


def _inline_skill_content(skill: Skill) -> str:
    frontmatter = _FRONTMATTER.match(skill.content)
    try:
        metadata = (yaml.safe_load(frontmatter.group(1)) or {}) if frontmatter else {}
    except yaml.YAMLError as error:
        raise ValueError(f"Skill {skill.name} has invalid frontmatter") from error
    if not isinstance(metadata, dict):
        raise ValueError(f"Skill {skill.name} frontmatter must be an object")
    metadata["name"] = skill.name
    metadata["description"] = skill.description or metadata.get("description") or skill.name
    body = skill.content[frontmatter.end():] if frontmatter else skill.content
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n" + body.rstrip() + "\n"


def _write_inline_skill(directory: Path, content: str) -> None:
    manifest = directory / "SKILL.md"
    if directory.resolve() != directory or manifest.is_symlink():
        raise RuntimeError("Inline Skill storage must not contain symbolic links")
    directory.mkdir(parents=True, exist_ok=True)
    if manifest.is_file() and manifest.read_text(encoding="utf-8") == content:
        return
    # 同一 Skill 可能被多个会话装载，以原子替换更新挂载目录中的清单。
    staged_path = None
    try:
        with NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8", delete=False) as file:
            staged_path = Path(file.name)
            file.write(content)
        staged_path.replace(manifest)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


async def service_skill_sources(names: list[str] | None, db: AsyncSession, *, materialize: bool = True) -> dict[str, Path]:
    """使用已安装目录和附属文件，或为控制台录入的内容生成清单。"""
    names = names or []
    if not isinstance(names, list):
        raise ValueError("Managed service requires a list of Skill names")
    for name in names:
        if not is_skill_name(name):
            raise ValueError(f"Invalid assigned Skill name: {name!r}")
    workspace = settings.workspace_root.resolve()
    shared_root = workspace / "skills"
    result: dict[str, Path] = {}
    for name in dict.fromkeys(names):
        installed = shared_root / name
        manifest = installed / "SKILL.md"
        if installed.resolve() != installed or manifest.is_symlink():
            raise RuntimeError(f"Assigned Skill {name} has a symbolic link for its installed directory or manifest")
        if manifest.is_file():
            result[name] = installed
            continue
        skill = await db.scalar(select(Skill).where(Skill.name == name))
        if skill is None or not skill.content.strip():
            raise RuntimeError(f"Assigned Skill {name} has no installed SKILL.md or registered instructions")
        content = _inline_skill_content(skill)
        directory = workspace / INLINE_SKILL_DIR / skill.id
        if materialize:
            _write_inline_skill(directory, content)
        result[name] = directory
    return result
