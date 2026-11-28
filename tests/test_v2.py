import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import Bot, Config, Store, TelegramError, repeats_reply
from daily_game import DailyGame, MOSCOW, clock_time
from internet_memes import InternetMemes, MemePage, safe_image, relevance, search_terms
from test_bot import FakeTelegram, FakeAgent, message


class InternetTests(unittest.TestCase):
    def test_cat_search_does_not_match_pronoun_kotoriy(self):
        self.assertEqual(relevance(search_terms('кот'), 'Одиссея, которую обсуждают все'), 0)
        self.assertGreater(relevance(search_terms('котов'), 'Кот кричит у двери'), 0)
        self.assertGreater(relevance(search_terms('работа'), 'Мемы про работу'), 0)
    def test_reject_untrusted_image_and_private_addresses(self):
        for url in ('http://i.redd.it/x.jpg', 'https://localhost/a.jpg', 'https://127.0.0.1/a.jpg',
                    'https://memepedia.ru.evil.test/a.jpg', 'https://user@i.redd.it/a.jpg',
                    'https://i.redd.it:8000/a.jpg', 'https://i.redd.it/a.html'):
            self.assertFalse(safe_image(url), url)
        self.assertTrue(safe_image('https://i.redd.it/test.png'))

    def test_html_extracts_gallery_not_logos_covers_or_templates(self):
        page = MemePage()
        page.feed('''<img src="https://memepedia.world/logo.png" class="logo">
            <h2 class="entry-title"><a href="https://memepedia.world/example/">Устал на работе</a></h2>
            <img src="https://memepedia.world/cover.jpg" class="wp-post-image">
            <img src="https://memepedia.world/ready.jpg" class="alignnone wp-image-12" alt="Готовый мем">
            <h2>Шаблон</h2><img src="https://memepedia.world/blank.jpg" class="wp-image-13">''')
        self.assertEqual(len(page.links), 1)
        self.assertEqual([x['url'] for x in page.images], ['https://memepedia.world/ready.jpg'])

    def test_gallery_also_accepts_plain_full_size_images(self):
        page = MemePage()
        page.feed('<img width="400" height="600" class="" src="https://memepedia.world/wp-content/uploads/ready.jpg">')
        self.assertEqual(len(page.images), 1)

    def test_repetition_guard(self):
        self.assertTrue(repeats_reply('Опять ты устроил тот же самый цирк, дружище!', ['Опять ты устроил тот же самый цирк, дружище.']))
        self.assertFalse(repeats_reply('Да', ['Да']))
        self.assertFalse(repeats_reply('Сегодня пойдём гулять, погода отличная.', ['Кому скинуть адрес ресторана?']))


class GameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = Store(self.path)
        self.game = DailyGame(self.store.db)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, chat, uid, **extra):
        self.game.observe(chat, {'id': uid, 'first_name': f'Друг {uid}', **extra})

    def test_result_and_statistics_survive_restart_and_no_reroll(self):
        self.add(-1, 1)
        self.add(-1, 2)
        first = self.game.choose(-1, '2026-11-28')
        self.assertIn(first['uid'], (1, 2))
        self.assertEqual(first['participants'], 2)
        self.add(-1, 3)
        self.assertEqual(first, self.game.choose(-1, '2026-11-28'))
        second = Store(self.path)
        try:
            game2 = DailyGame(second.db)
            self.assertEqual(first, game2.choose(-1, '2026-11-28'))
            self.assertEqual(sum(x['wins'] for x in game2.stats(-1)), 1)
            game2.choose(-1, '2026-11-29')
            self.assertEqual(sum(x['wins'] for x in game2.stats(-1)), 2)
        finally:
            second.db.close()

    def test_excludes_bots_left_opted_out_and_noroast(self):
        self.add(-1, 1)
        self.add(-1, 2)
        self.add(-1, 3, is_bot=True)
        self.add(-1, 4)
        self.game.observe(-1, {'id': 4, 'first_name': 'Вышел'}, active=False)
        self.add(-1, 5)
        self.game.opt_out(-1, 5, True)
        self.add(-1, 6)
        self.store.no_roast(-1, 6, 'Без стёба', True)
        self.assertEqual([x['uid'] for x in self.game.candidates(-1)], [1, 2])
        self.add(-1, 5)  # Новое сообщение не отменяет выход из игры.
        self.assertEqual([x['uid'] for x in self.game.candidates(-1)], [1, 2])

    def test_other_chats_and_too_few_members(self):
        self.add(-1, 1)
        self.add(-2, 2)
        self.assertIsNone(self.game.choose(-1, '2026-11-28'))
        self.assertEqual(self.game.stats(-2), [])

    def test_migration_archives_only_automatic_memory_and_bot_context_once(self):
        self.store.update_memory(-1, '- Старая шутка бота.')
        self.store.remember(-1, 'Настоящий закреплённый факт')
        self.store.record(-1, 0, 1, 99, 'Бот', 'Старая шутка')
        self.store.record(-1, 0, 2, 1, 'Человек', 'Реальный разговор')
        self.store.migrate_behavior(99)
        self.assertEqual(self.store.memory(-1), '')
        self.assertEqual(self.store.facts(-1), ['Настоящий закреплённый факт'])
        self.assertEqual(len(self.store.context(-1, 0, 40)), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM memory_archive').fetchone()[0], 1)
        self.store.update_memory(-1, '- Новая полезная память.')
        self.store.migrate_behavior(99)
        self.assertIn('Новая', self.store.memory(-1))

    def test_clock_validation(self):
        self.assertEqual(clock_time('21:30'), '21:30')
        for text in ('24:00', '12:60', '1:00', '12:00 UTC'):
            with self.assertRaises(ValueError):
                clock_time(text)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tg, self.agent = FakeTelegram(), FakeAgent()
        self.bot = Bot(Config('unused', 7, root, root / 'codex', chance=0), self.tg, self.agent)
        self.bot.me = {'id': 99, 'username': 'our_bot'}
        self.bot.store.allow(-100)
        self.bot.meme_finder.find = AsyncMock(return_value=[{'url': 'https://i.redd.it/ready.jpg', 'title': 'Устал', 'source': 'https://redd.it/source'}])

    async def asyncTearDown(self):
        tasks = list(self.bot.background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.bot.store.db.close()
        self.tmp.cleanup()

    async def test_command_fetches_real_photo_without_saved_catalog_or_ai(self):
        await self.bot.handle(message('/meme работа'))
        await asyncio.gather(*list(self.bot.background))
        self.assertTrue(any(method == 'sendPhoto' and data['photo'].endswith('/ready.jpg') for method, data in self.tg.sent))
        self.assertEqual(self.agent.payloads, [])
        self.assertEqual(self.bot.store.memes(-100), [])
        self.assertEqual(len(self.bot.game.recent_memes(-100)), 1)

    async def test_natural_language_reply_sends_internet_meme(self):
        self.agent.result = dict(self.agent.result, text='', meme_query='работа')
        await self.bot.handle(message('Кореш скинь мем про работу'))
        worker = asyncio.create_task(self.bot.worker())
        try:
            await asyncio.wait_for(self.bot.queue.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(any(method == 'sendPhoto' for method, _ in self.tg.sent))

    async def test_daily_without_codex_once_per_day_and_same_manual_result(self):
        self.bot.game.observe(-100, {'id': 7, 'first_name': 'Саша'})
        self.bot.game.observe(-100, {'id': 8, 'first_name': '😀 Миша'})
        now = datetime(2026, 11, 28, 11, 59, tzinfo=MOSCOW)
        await self.bot.daily_tick(now)
        self.assertEqual(self.bot.game.stats(-100), [])
        noon = now.replace(hour=12, minute=0)
        await self.bot.daily_tick(noon)
        await self.bot.daily_tick(noon)
        self.assertEqual(sum(x['wins'] for x in self.bot.game.stats(-100)), 1)
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.tg.sent), 1)
        await self.bot.draw(-100, message=message('/pidor'), now=noon)
        self.assertEqual(sum(x['wins'] for x in self.bot.game.stats(-100)), 1)
        self.assertEqual(self.agent.payloads, [])

    async def test_daily_send_failure_retries_same_winner(self):
        for uid in (7, 8, 9):
            self.bot.game.observe(-100, {'id': uid, 'first_name': str(uid)})
        original = self.tg.call
        async def fail_send(method, **kwargs):
            if method == 'sendMessage':
                raise TelegramError(500)
            return await original(method, **kwargs)
        self.tg.call = fail_send
        now = datetime(2026, 11, 28, 13, 0, tzinfo=MOSCOW)
        await self.bot.daily_tick(now)
        first = self.bot.game.result(-100, '2026-11-28')
        self.assertFalse(first['announced'])
        self.tg.call = original
        await self.bot.daily_tick(now)
        second = self.bot.game.result(-100, '2026-11-28')
        self.assertEqual(first['uid'], second['uid'])
        self.assertTrue(second['announced'])
        self.assertEqual(sum(x['wins'] for x in self.bot.game.stats(-100)), 1)

    async def test_schedule_permissions_membership_and_opt_out(self):
        await self.bot.handle(message('/pidortime 21:00', uid=8))
        self.assertEqual(self.bot.store.get('draw_time:-100'), '')
        await self.bot.handle(message('/pidortime 21:00', uid=7, mid=2))
        self.assertEqual(self.bot.store.get('draw_time:-100'), '21:00')
        await self.bot.handle(message('/pidorleave', uid=8, mid=3))
        self.assertNotIn(8, [x['uid'] for x in self.bot.game.candidates(-100)])
        await self.bot.membership({'chat': {'id': -100}, 'new_chat_member': {'status': 'left', 'user': {'id': 7, 'first_name': 'Саша'}}})
        self.assertEqual(self.bot.game.candidates(-100), [])


if __name__ == '__main__':
    unittest.main()
