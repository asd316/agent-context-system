PRAGMA foreign_keys = ON;

CREATE TABLE nodes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,          -- 显示为 n{id};AUTOINCREMENT 保证编号不复用
  name             TEXT NOT NULL CHECK (length(name) > 0),
  category         TEXT NOT NULL CHECK (category IN ('Reference','Knowledge','Project','Skill')),
  hier_summary     TEXT NOT NULL DEFAULT '' CHECK (length(hier_summary) <= 40),
  content_summary  TEXT CHECK (content_summary IS NULL OR length(content_summary) <= 200),
  path             TEXT UNIQUE,                                -- 挂载物;可为空
  path_kind        TEXT CHECK (path_kind IN ('file','dir')),
  status           TEXT,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  modified_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  -- 维护标签
  pending          INTEGER NOT NULL DEFAULT 0 CHECK (pending IN (0,1)),   -- 1=维护 agent 新建,待用户确认
  path_state       TEXT NOT NULL DEFAULT 'ok' CHECK (path_state IN ('ok','missing')),
  path_checked_at  TEXT,
  hier_by          TEXT CHECK (hier_by IN ('human','ai')),
  content_by       TEXT CHECK (content_by IN ('human','ai')),
  content_src_mtime TEXT,                                      -- 生成内容摘要时挂载物的修改时间
  CHECK ((path IS NULL) = (path_kind IS NULL)),
  CHECK (content_summary IS NULL OR path IS NOT NULL)
);

CREATE TABLE edges (
  parent_id  INTEGER NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
  child_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
  origin     TEXT NOT NULL CHECK (origin IN ('scan','manual')),
  PRIMARY KEY (parent_id, child_id),
  CHECK (parent_id <> child_id)
) WITHOUT ROWID;
CREATE INDEX edges_child ON edges(child_id);

CREATE TRIGGER edges_no_cycle BEFORE INSERT ON edges
WHEN EXISTS (
  WITH RECURSIVE anc(id) AS (
    SELECT NEW.parent_id
    UNION
    SELECT e.parent_id FROM edges e JOIN anc ON e.child_id = anc.id
  ) SELECT 1 FROM anc WHERE id = NEW.child_id
)
BEGIN SELECT RAISE(ABORT, 'cycle'); END;

CREATE VIRTUAL TABLE nodes_fts USING fts5(
  name, hier_summary, content_summary,
  content='nodes', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER nodes_ai AFTER INSERT ON nodes BEGIN
  INSERT INTO nodes_fts(rowid,name,hier_summary,content_summary) VALUES (new.id,new.name,new.hier_summary,new.content_summary);
END;
CREATE TRIGGER nodes_ad AFTER DELETE ON nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts,rowid,name,hier_summary,content_summary) VALUES ('delete',old.id,old.name,old.hier_summary,old.content_summary);
END;
CREATE TRIGGER nodes_au AFTER UPDATE ON nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts,rowid,name,hier_summary,content_summary) VALUES ('delete',old.id,old.name,old.hier_summary,old.content_summary);
  INSERT INTO nodes_fts(rowid,name,hier_summary,content_summary) VALUES (new.id,new.name,new.hier_summary,new.content_summary);
END;

-- 访问计数: 干活 agent 通过 get/children/ancestors 读到某节点就累加一次。运行遥测,不是索引内容,不进快照。
CREATE TABLE IF NOT EXISTS node_access (
  node_id  INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  hits     INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0),
  last_at  TEXT
);

-- 访问明细: 每次读命令一行 (含 search 的查询词),用来回看 agent 实际怎么用这套索引。不带外键,节点删除后历史仍在。
CREATE TABLE IF NOT EXISTS access_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  cmd     TEXT NOT NULL,
  node_id INTEGER,
  query   TEXT,
  cwd     TEXT
);

-- 共享 Category (shared/categories.yaml) 的 id 与本机节点的对应。由 scan 维护 (catalog.sync_index,老库在那里补建);节点编号只在本机有效,不进快照。
CREATE TABLE IF NOT EXISTS shared_categories (
  category_id TEXT PRIMARY KEY,
  node_id     INTEGER NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE CASCADE
);
