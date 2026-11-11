"""与作品历史共用数据库文件，独立连接提交桌面任务快照。"""

import json

import aiosqlite


class DesktopStore:
    def __init__(self, path):
        self.path = path
        self.conn = None

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA busy_timeout=5000")
        columns = await self.conn.execute("PRAGMA table_info(job)")
        if "desktop_payload" not in {row[1] for row in await columns.fetchall()}:
            await self.conn.execute("ALTER TABLE job ADD COLUMN desktop_payload TEXT")
        await self.conn.commit()

    async def save(self, job):
        await self.conn.execute(
            "INSERT INTO job(job_id,url,status,created_at,desktop_payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, desktop_payload=excluded.desktop_payload",
            (job["job_id"], job["target"].get("url", ""), job["status"], job["created_at"],
             json.dumps(job, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def load(self):
        cursor = await self.conn.execute(
            "SELECT desktop_payload FROM job WHERE desktop_payload IS NOT NULL ORDER BY created_at DESC"
        )
        result = []
        for row in await cursor.fetchall():
            result.append(json.loads(row[0]))
        return result

    async def close(self):
        if self.conn:
            await self.conn.close()
