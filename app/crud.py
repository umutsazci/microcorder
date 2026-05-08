from app.models import User, Task, TaskStatus


def create_user(session, email: str, hashed_password: str, credits: int = 0) -> User:
    user = User(email=email, hashed_password=hashed_password, credits=credits)
    session.add(user)
    session.flush()
    return user


def get_user_by_email(session, email: str) -> User | None:
    return session.query(User).filter(User.email == email).one_or_none()


def create_task(session, user_id: int) -> Task:
    task = Task(user_id=user_id, status=TaskStatus.PENDING)
    session.add(task)
    session.flush()
    return task


def get_task(session, task_id: int) -> Task | None:
    return session.get(Task, task_id)


def delete_user(session, user_id: int) -> None:
    user = session.get(User, user_id)
    if user is not None:
        session.delete(user)
        session.flush()
