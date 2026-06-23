"""Recipe and API-library routes.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET /recipes/{slug}       — public recipe detail
  GET /api-library/{slug}   — API library entry detail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import APILibraryEntry, Recipe
from app.schemas import APILibraryOut, RecipeOut

router = APIRouter(tags=["recipes"])


@router.get("/recipes/{slug}", response_model=RecipeOut, tags=["recipes"])
def get_recipe(slug: str, db: Session = Depends(get_db)):
    """Return public recipe detail."""
    recipe = (
        db.query(Recipe)
        .options(joinedload(Recipe.creator))
        .filter(Recipe.slug == slug, Recipe.is_public == True)
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail=f"Recipe '{slug}' not found")

    return RecipeOut(
        id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        description=recipe.description,
        content=recipe.content,
        category=recipe.category,
        creator_name=recipe.creator.name if recipe.creator else None,
        created_at=recipe.created_at,
        updated_at=recipe.updated_at,
    )


@router.get("/api-library/{slug}", response_model=APILibraryOut, tags=["api-library"])
def get_api_library_entry(slug: str, db: Session = Depends(get_db)):
    """Return API library entry detail."""
    entry = db.query(APILibraryEntry).filter(APILibraryEntry.slug == slug).first()
    if not entry:
        raise HTTPException(status_code=404, detail=f"API library entry '{slug}' not found")
    return entry
