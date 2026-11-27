import asyncio
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import (Bot, CodexAgent, Config, Job, Store, child_env, command,
                 directed, extract_text, safe_document)


def message(text='', uid=7, chat=-100, private=False, mid=1, **extra):
    return {'message_id': mid, 'date': int(time.time()),
            'chat': {'id': chat, 'type': 'private' if private else 'supergroup'},
            'from': {'id': uid, 'first_name': 'Друг', 'is_bot': False},
            'text': text, **extra}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.counter = 100

    async def call(self, method, **payload):
        self.sent.append((method, payload))
        self.counter += 1
        return {'message_id': self.counter}

    async def document(self, payload, filename, content):
        self.sent.append(('sendDocument', dict(payload, filename=filename, content=content)))


class FakeAgent:
    active = None

    def __init__(self):
        self.payloads = []
        self.result = {'text': 'Ну ты дал.', 'meme_id': None, 'document': None}

    async def run(self, payload, images):
        self.payloads.append(payload)
        return self.result

    async def auth_status(self):
        return True


class ParsingTests(unittest.TestCase):
    def test_command_target(self):
        self.assertEqual(command('/quiet@our_bot', 'our_bot'), ('quiet', ''))
        self.assertEqual(command('/quiet@other_bot', 'our_bot'), ('', ''))
        self.assertEqual(command('/remember несколько\nслов', 'our_bot'), ('remember', 'несколько\nслов'))

    def test_direct_mentions_names_replies(self):
        for text in ('Кореш, привет', 'Эй @our_bot', '/meme@our_bot'):
            self.assertTrue(directed(message(text), 99, 'our_bot', ('кореш',)))
        for text in ('закорешились', 'у @our_bot_extra спроси', '/help@other_bot'):
            self.assertFalse(directed(message(text), 99, 'our_bot', ('кореш',)))
        self.assertTrue(directed(message('угу', reply_to_message={'from': {'id': 99}}), 99, 'our_bot', ()))

    def test_document_cannot_escape_or_execute(self):
        for name in ('../../secret.txt', 'auth.json\r\nX: bad', '.env', 'go.py', '.hidden.txt', 'x..txt'):
            self.assertIsNone(safe_document({'filename': name, 'content': 'a'}))
        self.assertEqual(safe_document({'filename': 'план.md', 'content': 'привет'}), ('план.md', 'привет'.encode()))
        self.assertIsNone(safe_document({'filename': 'a.txt', 'content': 'x' * 20001}))

    def test_docx_text_without_running_content(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Привет</w:t></w:r></w:p></w:document>')
        self.assertEqual(extract_text('a.docx', data.getvalue()), 'Привет')
        with self.assertRaises(ValueError):
            extract_text('a.exe', b'bad')

    def test_child_environment_drops_secrets(self):
        with patch.dict(os.environ, {'BOT_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret'}):
            cfg = Config('test', 7, Path('data'), Path('data/codex'))
            env = child_env(cfg)
            self.assertFalse(set(('BOT_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY')) & set(env))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_chat_and_topic_isolation_and_persistence(self):
        for chat, topic, mid, text in ((-1, 0, 1, 'first'), (-2, 0, 2, 'secret'), (-1, 9, 3, 'topic')):
            self.store.record(chat, topic, mid, 7, 'Name', text)
        self.store.remember(-2, 'private note')
        self.store.save_meme(-2, 'photo', 'secret-file', 'label')
        self.assertEqual([x['text'] for x in self.store.context(-1, 0, 40)], ['first'])
        self.assertEqual(self.store.facts(-1), [])
        self.assertEqual(self.store.memes(-1), [])
        self.store.allow(-1)
        second = Store(self.path)
        try:
            self.assertTrue(second.allowed(-1))
            self.assertEqual(second.context(-1, 9, 40)[0]['text'], 'topic')
        finally:
            second.db.close()

    def test_retention_and_forget(self):
        for mid in range(305):
            self.store.record(-1, 0, mid, 7, 'a', str(mid))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 300)
        self.store.remember(-1, 'remember me')
        self.store.no_roast(-1, 7, 'Name', True)
        self.store.forget(-1)
        self.assertEqual(self.store.context(-1, 0, 10), [])
        self.assertEqual(self.store.facts(-1), [])
        self.assertEqual(self.store.protected(-1), [])

    def test_budget_survives_restart(self):
        self.assertTrue(self.store.reserve_turn(1))
        second = Store(self.path)
        try:
            self.assertFalse(second.reserve_turn(1))
        finally:
            second.db.close()

    def test_markdown_memory_persists_and_forget_clears_it(self):
        self.store.update_memory(-1, '- Саша любит настолки.\n- «Кубик обиделся» — внутренняя шутка.')
        self.store.remember(-1, 'По пятницам собираемся в 20:00.')
        memory_file = self.path.parent / 'chats' / '-1' / 'memory.md'
        content = memory_file.read_text(encoding='utf-8')
        self.assertIn('Саша любит настолки', content)
        self.assertIn('По пятницам собираемся', content)
        self.assertEqual(self.store.memory(-2), '')
        second = Store(self.path)
        try:
            self.assertIn('Саша', second.memory(-1))
        finally:
            second.db.close()
        self.store.forget(-1)
        self.assertNotIn('Саша', memory_file.read_text(encoding='utf-8'))
        self.assertNotIn('По пятницам', memory_file.read_text(encoding='utf-8'))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config('test', 7, root, root / 'codex', chance=0)
        self.tg, self.agent = FakeTelegram(), FakeAgent()
        self.bot = Bot(self.cfg, self.tg, self.agent)
        self.bot.me = {'id': 99, 'username': 'our_bot'}

    async def asyncTearDown(self):
        self.bot.store.db.close()
        self.tmp.cleanup()

    async def drain(self):
        worker = asyncio.create_task(self.bot.worker())
        try:
            await asyncio.wait_for(self.bot.queue.join(), 2)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def test_unapproved_group_and_other_private_users_silent(self):
        await self.bot.handle(message('Кореш привет'))
        await self.bot.handle(message('/login', uid=8, private=True, chat=8))
        self.assertEqual(self.tg.sent, [])
        self.assertTrue(self.bot.queue.empty())

    async def test_owner_allows_group_others_cannot(self):
        await self.bot.handle(message('/allow', uid=8))
        self.assertFalse(self.bot.store.allowed(-100))
        await self.bot.handle(message('/allow'))
        self.assertTrue(self.bot.store.allowed(-100))

    async def test_context_and_reply_end_to_end(self):
        self.bot.store.allow(-100)
        self.bot.store.record(-200, 0, 2, 8, 'secret', 'OTHER CHAT SECRET')
        await self.bot.handle(message('Вчера играли в настолки', mid=2))
        await self.bot.handle(message('Кореш, во что мы играли?', mid=3))
        await self.drain()
        self.assertEqual(len(self.agent.payloads), 1)
        payload = self.agent.payloads[0]
        self.assertIn('настолки', json.dumps(payload, ensure_ascii=False))
        self.assertNotIn('OTHER CHAT SECRET', json.dumps(payload))
        replies = [p for method, p in self.tg.sent if method == 'sendMessage']
        self.assertEqual(replies[-1]['text'], 'Ну ты дал.')
        self.assertEqual(replies[-1]['reply_parameters']['message_id'], 3)

    async def test_no_roast_and_unauthorized_admin_command(self):
        self.bot.store.allow(-100)
        await self.bot.handle(message('/noroast', uid=8))
        await self.bot.handle(message('/forget', uid=8, mid=2))
        self.assertEqual(self.bot.store.protected(-100)[0]['user_id'], 8)
        await self.bot.handle(message('Кореш, привет', mid=3))
        await self.drain()
        self.assertEqual(self.agent.payloads[0]['noroast_users'][0]['user_id'], 8)

    async def test_forget_invalidates_pending_response(self):
        self.bot.store.allow(-100)
        await self.bot.handle(message('Кореш, секрет'))
        await self.bot.handle(message('/forget', mid=2))
        await self.drain()
        self.assertEqual(self.agent.payloads, [])

    async def test_quiet_suppresses_spontaneous_but_not_direct(self):
        self.bot.cfg = dataclasses.replace(self.cfg, chance=0.25)
        self.bot.store.allow(-100)
        await self.bot.handle(message('/quiet'))
        with patch('bot.random.random', return_value=0):
            await self.bot.handle(message('Ахаха', mid=2))
        self.assertTrue(self.bot.queue.empty())
        await self.bot.handle(message('Кореш привет', mid=3))
        await self.drain()
        self.assertEqual(len(self.agent.payloads), 1)

    async def test_spontaneous_cooldown_and_empty_response(self):
        self.bot.cfg = dataclasses.replace(self.cfg, chance=0.25)
        self.bot.store.allow(-100)
        self.agent.result = {'text': '', 'meme_id': None, 'document': None}
        with patch('bot.random.random', return_value=0):
            await self.bot.handle(message('Смешная история', mid=2))
            await self.drain()
            await self.bot.handle(message('И ещё одна', mid=3))
        self.assertTrue(self.bot.queue.empty())
        self.assertEqual(self.agent.payloads[0]['mode'], 'spontaneous')
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.tg.sent))

    async def test_meme_isolation_and_bad_model_id(self):
        self.bot.store.allow(-100)
        other = self.bot.store.save_meme(-200, 'photo', 'PRIVATE', 'secret')
        self.agent.result = {'text': '', 'meme_id': other, 'document': None}
        await self.bot.handle(message('Кореш скинь мем'))
        await self.drain()
        self.assertFalse(any(method == 'sendPhoto' for method, _ in self.tg.sent))

    async def test_save_and_send_group_meme(self):
        self.bot.store.allow(-100)
        await self.bot.handle(message('/savememe когда устал', reply_to_message={'photo': [{'file_id': 'PHOTO'}]}))
        await self.bot.handle(message('/meme устал', mid=2))
        self.assertEqual(self.tg.sent[-1][0], 'sendPhoto')
        self.assertEqual(self.tg.sent[-1][1]['photo'], 'PHOTO')

    async def test_reject_bot_loops_old_updates_and_anonymous_admin(self):
        self.bot.store.allow(-100)
        await self.bot.handle(message('Кореш', **{'from': {'id': 8, 'is_bot': True}}))
        await self.bot.handle(message('Кореш', date=int(time.time()) - 300))
        await self.bot.handle(message('/allow', sender_chat={'id': -100}))
        self.assertTrue(self.bot.queue.empty())

    async def test_topic_reply_preserved(self):
        self.bot.store.allow(-100)
        await self.bot.handle(message('Кореш', message_thread_id=11))
        await self.drain()
        reply = [p for method, p in self.tg.sent if method == 'sendMessage'][-1]
        self.assertEqual(reply['message_thread_id'], 11)

    async def test_automatic_memory_updates_and_memory_command_exports_it(self):
        self.bot.store.allow(-100)
        self.agent.result = dict(self.agent.result, memory='- Друг любит настолки.')
        await self.bot.handle(message('Кореш, я люблю настолки'))
        await self.drain()
        self.assertIn('настолки', self.bot.store.memory(-100))
        await self.bot.handle(message('/memory', mid=2))
        self.assertEqual(self.tg.sent[-1][0], 'sendDocument')
        self.assertIn('настолки', self.tg.sent[-1][1]['content'].decode())

    async def test_foreign_bot_command_never_triggers_spontaneous(self):
        self.bot.cfg = dataclasses.replace(self.cfg, chance=0.25)
        self.bot.store.allow(-100)
        with patch('bot.random.random', return_value=0):
            await self.bot.handle(message('/help@other_bot'))
        self.assertTrue(self.bot.queue.empty())

    async def test_ephemeral_owner_notice_not_in_model_context(self):
        m = message('/status', private=True, chat=7)
        await self.bot.handle(m)
        self.assertEqual(self.bot.store.context(7, 0, 40), [])


if __name__ == '__main__':
    unittest.main()
