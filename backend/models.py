from typing import Literal
from pydantic import BaseModel, Field, model_validator


class Scene(BaseModel):
    type: Literal["title_card", "diagram"]
    diagram_kind: Literal["flow", "cycle", "network", "layers"] = "flow"
    heading: str = Field(min_length=1, max_length=120)
    narration: str = Field(min_length=5, max_length=700)
    items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check(self):
        if self.type == "diagram":
            self.items = [i.strip() for i in self.items if i.strip()][:6]
            if len(self.items) < 2 or any(len(i) > 40 for i in self.items):
                raise ValueError("diagram needs 2-6 short items")
        return self


class Plan(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    level: Literal["beginner", "intermediate"]
    scenes: list[Scene] = Field(min_length=3, max_length=6)

    @model_validator(mode="after")
    def require_diagram(self):
        if not any(scene.type == "diagram" for scene in self.scenes):
            raise ValueError("a lesson needs at least one topic-specific diagram")
        return self


class GenerateIn(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    level: Literal["beginner", "intermediate"] = "beginner"
    language: str = "en"
    length: int = Field(default=60, ge=20, le=180)
    voice: str | None = None


class RevoiceIn(BaseModel):
    voice: str | None = None
