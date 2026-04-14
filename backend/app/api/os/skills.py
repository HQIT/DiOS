"""Skills CRUD + Git 导入：OS 严选的 Skills 仓库。"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.tables import Skill
from app.models.schemas import SkillCreate, SkillUpdate, SkillOut

router = APIRouter(prefix="/skills", tags=["skills"])
logger = logging.getLogger(__name__)

KNOWN_SKILL_REPOS: list[dict[str, str]] = [
    {"name": "cursor-tools", "url": "https://github.com/eastlondoner/cursor-tools", "description": "Browser, GitHub, documentation and code generation tools for Cursor AI"},
    {"name": "elevenlabs-mcp", "url": "https://github.com/nicobailon/elevenlabs-mcp-cursor-skill", "description": "ElevenLabs text-to-speech integration"},
    {"name": "ui-ux-pro-max", "url": "https://github.com/5pungus/ui-ux-pro-max-SKILL-cursor", "description": "Advanced UI/UX design workflow skill"},
]


@router.get("", response_model=list[SkillOut])
async def list_skills(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Skill).order_by(Skill.name))
    return result.scalars().all()


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)):
    skill = Skill(name=body.name, description=body.description, source_url=body.source_url, content=body.content)
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.get("/registry")
async def search_registry(q: str = ""):
    """搜索已知的 Skill 仓库列表。"""
    if q:
        q_lower = q.lower()
        matched = [r for r in KNOWN_SKILL_REPOS if q_lower in r["name"].lower() or q_lower in r["description"].lower()]
    else:
        matched = KNOWN_SKILL_REPOS
    return {"repos": matched, "total": len(matched)}


@router.post("/import-git", response_model=SkillOut, status_code=201)
async def import_from_git(
    url: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
):
    """从 Git 仓库导入 Skill：clone → 读 SKILL.md → 存 DB → 复制到 workspace/skills/。"""
    repo_name = _repo_name_from_url(url)

    existing = await db.execute(select(Skill).where(Skill.source_url == url))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Skill from {url} already imported")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, tmpdir],
                check=True, capture_output=True, timeout=60,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(400, f"Git clone failed: {e.stderr.decode()[:500]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "Git clone timed out")

        name, description, content = _parse_skill_dir(Path(tmpdir), repo_name)

        skills_dir = settings.workspace_root / "skills" / name
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        shutil.copytree(tmpdir, skills_dir, dirs_exist_ok=True)

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
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(skill, k, v)
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
    m = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return m.group(1) if m else "unknown-skill"


def _parse_skill_dir(path: Path, fallback_name: str) -> tuple[str, str, str]:
    """从 SKILL.md 或 README.md 中提取 name/description/content。"""
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        for candidate in path.rglob("SKILL.md"):
            skill_md = candidate
            break

    content = ""
    name = fallback_name
    description = ""

    target = skill_md if skill_md.exists() else path / "README.md"
    if target.exists():
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            pass

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not name:
            name = stripped.lstrip("# ").strip() or fallback_name
        if stripped.startswith("description:"):
            description = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            break

    if not description:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                description = stripped[:200]
                break

    return name, description, content
