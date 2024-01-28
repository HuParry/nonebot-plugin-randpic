from nonebot.adapters import Event
from nonebot.params import Depends
from nonebot import require, on_message
from sqlalchemy.orm import Mapped, mapped_column

require("nonebot_plugin_orm")
from nonebot_plugin_orm import Model, async_scoped_session

matcher = on_message()


def get_user_id(event: Event) -> str:
    return event.get_user_id()


class User(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = Depends(get_user_id)


@matcher.handle()
async def _(event: Event, sess: async_scoped_session, user: User | None):
    if user:
        await matcher.finish(f"Hello, {user.user_id}")

    sess.add(User(user_id=get_user_id(event)))
    await sess.commit()
    await matcher.finish("Hello, new user!")