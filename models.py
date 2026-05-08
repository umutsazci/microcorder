"""ORM models for the core backend. Single `User` table — auth + credits."""
from sqlalchemy import Column, Integer, String
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    # New users start at 5 credits per the spec; debited 1 per /record call.
    credits = Column(Integer, nullable=False, default=5, server_default="5")
