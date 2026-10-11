from pydantic import BaseModel, validator


class User(BaseModel):
    id: int
    email: str

    @validator("email")
    def lower(cls, v):
        return v.lower()

    class Config:
        orm_mode = True


def to_payload(user: User) -> dict:
    return user.dict(exclude_none=True)


def from_payload(data: dict) -> User:
    return User.parse_obj(data)
