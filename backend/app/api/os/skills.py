"""Skills CRUD + Git 导入：按 Agent Skills 规范校验并注册到单机工作区。"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.schemas import SkillCreate, SkillOut, SkillUpdate
from app.models.tables import Skill

router = APIRouter(prefix="/skills", tags=["skills"])
logger = logging.getLogger(__name__)

KNOWN_SKILL_REPOS: list[dict[str, str]] = [
    {
        "name": "mcp-builder",
        "url": "https://github.com/anthropics/skills/tree/main/skills/mcp-builder",
        "description": "Create well-designed MCP servers with reliable tools and schemas.",
    },
    {
        "name": "frontend-design",
        "url": "https://github.com/anthropics/skills/tree/main/skills/frontend-design",
        "description": "Build polished production frontend interfaces with intentional visual design.",
    },
    {
        "name": "webapp-testing",
        "url": "https://github.com/anthropics/skills/tree/main/skills/webapp-testing",
        "description": "Test local web applications and inspect browser behavior.",
    },
]


@router.get("", response_model=list[SkillOut])
async def list_skills(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Skill).order_by(Skill.name))
    return result.scalars().all()


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)):
    skill = Skill(
        name=body.name,
        description=body.description,
        source_url=body.source_url,
        content=body.content,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.get("/registry")
async def search_registry(q: str = ""):
    """搜索 DiOS 维护的 Agent Skills 候选来源。"""
    query = q.strip().lower()
    matched = [
        repo for repo in KNOWN_SKILL_REPOS
        if not query
        or query in repo["name"].lower()
        or query in repo["description"].lower()
    ]
    return {"repos": matched, "total": len(matched), "source": "dios-curated"}


@router.post("/import-git", response_model=SkillOut, status_code=201)
async def import_from_git(
    url: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
):
    """浅克隆 Git 来源，校验一个明确的 SKILL.md，再注册到工作区。"""
    clone_url, ref, requested_path = _resolve_git_source(url)
    fallback_name = Path(requested_path).name if requested_path else _repo_name_from_url(clone_url)

    existing = await db.execute(select(Skill).where(Skill.source_url == url))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Skill from {url} already imported")

    with tempfile.TemporaryDirectory() as tmpdir:
        clone_command = ["git", "clone", "--depth", "1"]
        if ref:
            clone_command.extend(["--branch", ref])
        clone_command.extend([clone_url, tmpdir])
        try:
            subprocess.run(clone_command, check=True, capture_output=True, timeout=60)
        except subprocess.CalledProcessError as error:
            raise HTTPException(400, f"Git clone failed: {error.stderr.decode()[:500]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "Git clone timed out")

        try:
            source_dir = _locate_skill_dir(Path(tmpdir), requested_path)
            name, description, content = _parse_skill_dir(source_dir, fallback_name)
        except ValueError as error:
            raise HTTPException(400, str(error))

        target_dir = settings.workspace_root / "skills" / name
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    skill = Skill(name=name, description=description, source_url=url, content=content)
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: str, db: AsyncSession = Depends(get_db)):
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


@router.put("/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: str, body: SkillUpdate, db: AsyncSession = Depends(get_db)):
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(skill, key, value)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, db: AsyncSession = Depends(get_db)):
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    skill_dir = settings.workspace_root / "skills" / skill.name
    if skill_dir.is_dir():
        shutil.rmtree(skill_dir, ignore_errors=True)
    await db.delete(skill)
    await db.commit()


def _repo_name_from_url(url: str) -> str:
    match = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return match.group(1) if match else "unknown-skill"


def _resolve_git_source(url: str) -> tuple[str, str | None, str | None]:
    """将 GitHub tree URL 拆成可 clone 的仓库、ref 和 Skill 子目录。"""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname == "github.com" and len(parts) >= 5 and parts[2] == "tree":
        owner, repository, _, ref, *subpath = parts
        clone_url = f"https://github.com/{owner}/{repository}.git"
        return clone_url, ref, "/".join(subpath)
    return url, None, None


def _locate_skill_dir(repo_root: Path, requested_path: str | None) -> Path:
    root = repo_root.resolve()
    if requested_path:
        candidate = (repo_root / requested_path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_dir():
            raise ValueError("Skill 子目录不存在或超出仓库范围")
        if not (candidate / "SKILL.md").is_file():
            raise ValueError("所选目录缺少 SKILL.md")
        return candidate

    candidates = sorted(path.parent for path in repo_root.rglob("SKILL.md"))
    if not candidates:
        raise ValueError("仓库中未找到 SKILL.md")
    if len(candidates) > 1:
        raise ValueError("仓库包含多个 Skill，请使用 GitHub tree URL 指定一个 Skill 子目录")
    return candidates[0]


def _parse_skill_dir(path: Path, fallback_name: str) -> tuple[str, str, str]:
    """读取并校验 Agent Skills 所需的 YAML frontmatter。"""
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError("Skill 目录缺少 SKILL.md")
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"无法读取 SKILL.md: {error}")

    frontmatter_match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", content, re.DOTALL)
    if not frontmatter_match:
        raise ValueError("SKILL.md 必须包含 YAML frontmatter")
    try:
        metadata = yaml.safe_load(frontmatter_match.group(1)) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"SKILL.md frontmatter 无效: {error}")
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter 必须是键值对象")

    name = str(metadata.get("name") or fallback_name).strip()
    description = str(metadata.get("description") or "").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("Skill name 必须为不超过 64 个字符的小写字母、数字和连字符")
    if not description or len(description) > 1024:
        raise ValueError("Skill description 必须为 1–1024 个字符")
    return name, description, content
