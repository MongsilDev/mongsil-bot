import discord
from discord import app_commands
from discord.ext import commands
from client import ERClient

from utils.errors import handle_errors
from utils.animal_utils import send_animal_photo


class Cat(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="고양이", description="고양이 사진")
    @handle_errors(user_message="고양이 사진을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def cat_command(self, interaction: discord.Interaction):
        """무작위 고양이 사진을 보여줍니다."""
        await send_animal_photo(interaction, 'https://api.thecatapi.com/v1/images/search', '고양이', 'cat')


async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Cat(client))
