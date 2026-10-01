"""SQLite storage. Each request owns a connection and a transaction."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "agents.sqlite3"


@contextmanager
def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                description TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role = 'user'),
                content TEXT NOT NULL CHECK(length(trim(content)) > 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS conversations_agent ON conversations(agent_id, id);
            CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
        """)
        connection.execute('BEGIN IMMEDIATE')
        # Existing agents, conversations and messages stay intact.
        connection.execute("""CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'yandex',
            folder_id TEXT NOT NULL,
            secret_ref TEXT NOT NULL,
            models_json TEXT NOT NULL DEFAULT '[]',
            models_updated_at TEXT,
            models_folder_id TEXT
        )""")
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(agents)')}
        for name in ('web_search', 'code_interpreter'):
            if name not in columns:
                connection.execute(f'ALTER TABLE agents ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0 CHECK({name} IN (0,1))')
        if 'connection_id' not in columns:
            connection.execute('ALTER TABLE agents ADD COLUMN connection_id INTEGER REFERENCES connections(id) ON DELETE RESTRICT')
        if 'model_id' not in columns:
            connection.execute('ALTER TABLE agents ADD COLUMN model_id TEXT')
        if connection.execute('PRAGMA user_version').fetchone()[0] < 3:
            # SQLite cannot alter a CHECK constraint. Copy inside one transaction,
            # retaining primary keys, timestamps and every existing message.
            connection.execute("""CREATE TABLE messages_v3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL CHECK(length(trim(content)) > 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            connection.execute('INSERT INTO messages_v3 SELECT * FROM messages')
            connection.execute('DROP TABLE messages')
            connection.execute('ALTER TABLE messages_v3 RENAME TO messages')
            connection.execute('CREATE INDEX messages_conversation ON messages(conversation_id, id)')
            connection.execute('PRAGMA user_version = 3')
        connection.execute("""CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
            error TEXT,
            finish_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_generation_per_conversation ON generations(conversation_id) WHERE status='running'")
        generation_columns = {row['name'] for row in connection.execute('PRAGMA table_info(generations)')}
        for name in ('started_at', 'finished_at', 'diagnostics_json'):
            if name not in generation_columns:
                connection.execute(f'ALTER TABLE generations ADD COLUMN {name} TEXT')
