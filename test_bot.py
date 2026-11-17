import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from config import settings
from database import db

async def main():
    # 1. Инициализируем базу данных (создадутся таблицы из Этапа 1)
    await db.init_db_async()
    print(f"База данных {settings.DB_PATH} успешно инициализирована.")
    
    # 2. Подключаем бота
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    # 3. Пишем простой обработчик для теста
    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        await message.answer(
            f"✅ Тестовый запуск успешен!\n\n"
            f"Твой ID: {message.from_user.id}\n"
            f"ID админа из конфига: {settings.LOG_CHAT_ID}\n"
            f"IP панели Dahua: {settings.DAHUA_IP}"
        )

    print("Тестовый бот запущен. Напиши ему /start в Telegram.")
    
    # 4. Запускаем long-polling
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())