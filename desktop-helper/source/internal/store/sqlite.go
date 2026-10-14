package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"path/filepath"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"

	// 纯 Go 的 SQLite 驱动，不需要 cgo，交叉编译简单。
	// 需要先：go get modernc.org/sqlite
	_ "modernc.org/sqlite"
)

// SQLiteStore 是生产用的事件队列。
//
// 为什么必须落盘：本代理的设计前提是「事件不丢」。
// 用户断网、服务端重启、代理自身被重启——任何一种情况下，
// 内存队列都会把还没上报的事件丢掉，而这些恰恰是最需要留痕的事件。
type SQLiteStore struct {
	db   *sql.DB
	path string
}

// schema 是本地队列的全部结构。
//
// 两个设计要点：
//
//  1. payload 存原始 JSON，上报时直接发出去，不再从列里重新拼装。
//     这样即使以后加了字段，老事件的 payload 依然是当初的样子，
//     不会因为代码升级而被"追认"上不存在的字段。
//
//  2. undos 表单独存处置的撤销凭据。它和事件分开，因为：
//     撤销凭据丢了 = 用户的文件永远还原不了，这比丢一条事件严重得多。
const schema = `
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    time_utc     TEXT NOT NULL,
    activity_id  INTEGER NOT NULL,
    sha256       TEXT,
    hashed       INTEGER NOT NULL,
    path         TEXT NOT NULL,
    old_path     TEXT,
    file_name    TEXT,
    size         INTEGER,
    collector    TEXT NOT NULL,
    category     TEXT,
    process_name TEXT,
    payload      TEXT NOT NULL,
    sent         INTEGER NOT NULL DEFAULT 0,
    created_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_unsent ON events(sent, created_utc);

CREATE TABLE IF NOT EXISTS undos (
    action_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    original   TEXT NOT NULL,
    applied    TEXT,
    previous   TEXT,
    payload    TEXT NOT NULL,
    at_utc     TEXT NOT NULL,
    reversed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_undos_live ON undos(reversed, at_utc);
`

func OpenSQLite(dir string) (*SQLiteStore, error) {
	path := filepath.Join(dir, "queue.db")
	db, err := sql.Open("sqlite", path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	// SQLite 单写者，连接数压到 1 可以彻底避免写冲突。
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return &SQLiteStore{db: db, path: path}, nil
}

func (s *SQLiteStore) Open() error  { return nil }
func (s *SQLiteStore) Close() error { return s.db.Close() }

func (s *SQLiteStore) Enqueue(ev model.FileEvent) error {
	payload, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	_, err = s.db.Exec(`
        INSERT OR IGNORE INTO events
          (event_id, time_utc, activity_id, sha256, hashed, path, old_path,
           file_name, size, collector, category, process_name, payload, sent, created_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)`,
		ev.EventID, ev.Time.UTC().Format(time.RFC3339Nano), int(ev.ActivityID),
		ev.Sha256, int(ev.Hashed), ev.Path, ev.OldPath, ev.FileName, ev.Size,
		string(ev.Collector), ev.Category, ev.ProcessName, string(payload),
		time.Now().UTC().Format(time.RFC3339Nano),
	)
	return err
}

func (s *SQLiteStore) DequeueBatch(n int) ([]model.FileEvent, error) {
	rows, err := s.db.Query(
		`SELECT payload FROM events WHERE sent = 0 ORDER BY created_utc LIMIT ?`, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make([]model.FileEvent, 0, n)
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		var ev model.FileEvent
		if err := json.Unmarshal([]byte(payload), &ev); err != nil {
			// 单条坏数据不应该让整批上报失败。
			continue
		}
		out = append(out, ev)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) MarkSent(ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`UPDATE events SET sent = 1 WHERE event_id = ?`)
	if err != nil {
		return err
	}
	defer stmt.Close()

	for _, id := range ids {
		if _, err := stmt.Exec(id); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *SQLiteStore) Depth() (int, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(*) FROM events WHERE sent = 0`).Scan(&n)
	return n, err
}

// Prune 清理已上报且超过保留期的事件。
//
// 只清理 sent=1 的——没上报的事件无论多老都不能删。
func (s *SQLiteStore) Prune(olderThan time.Duration) (int, error) {
	cutoff := time.Now().UTC().Add(-olderThan).Format(time.RFC3339Nano)
	res, err := s.db.Exec(`DELETE FROM events WHERE sent = 1 AND created_utc < ?`, cutoff)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// PendingHash 返回还没算哈希的未上报事件。
//
// 只取 hashed=0（"还没算"），不取 -1（"算失败了"）——
// 后者通常是权限不足或被独占，重试也不会成功，不该反复消耗 IO。
func (s *SQLiteStore) PendingHash(limit int) ([]model.FileEvent, error) {
	rows, err := s.db.Query(
		`SELECT payload FROM events
          WHERE sent = 0 AND hashed = ?
          ORDER BY created_utc LIMIT ?`,
		int(model.HashedNo), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []model.FileEvent
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		var ev model.FileEvent
		if err := json.Unmarshal([]byte(payload), &ev); err != nil {
			continue
		}
		out = append(out, ev)
	}
	return out, rows.Err()
}

// SetHash 给一条已入队的事件补上哈希。
//
// 注意要同时更新列和 payload：上报时发的是 payload，
// 列只用于查询。只改一处会导致「查得到但上报的是旧值」这种诡异问题。
func (s *SQLiteStore) SetHash(eventID, sha256 string, hashed model.HashedState) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var payload string
	err = tx.QueryRow(`SELECT payload FROM events WHERE event_id = ?`, eventID).Scan(&payload)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNotFound
		}
		return err
	}

	var ev model.FileEvent
	if err := json.Unmarshal([]byte(payload), &ev); err != nil {
		return err
	}
	ev.Sha256 = sha256
	ev.Hashed = hashed

	newPayload, err := json.Marshal(ev)
	if err != nil {
		return err
	}

	if _, err := tx.Exec(
		`UPDATE events SET sha256 = ?, hashed = ?, payload = ? WHERE event_id = ?`,
		sha256, int(hashed), string(newPayload), eventID); err != nil {
		return err
	}
	return tx.Commit()
}

// ---------------------------------------------------------------------------
// 撤销凭据（Undo）的持久化
//
// 单独一组方法，因为它的重要性高于事件：
// 撤销凭据丢失 = 用户被隔离/锁权限的文件永远还原不了。
// ---------------------------------------------------------------------------

func (s *SQLiteStore) SaveUndo(u model.ActionKind, actionID, original, applied, previous string) error {
	payload, err := json.Marshal(map[string]string{
		"original": original, "applied": applied, "previous": previous,
	})
	if err != nil {
		return err
	}
	_, err = s.db.Exec(`
        INSERT OR REPLACE INTO undos
          (action_id, kind, original, applied, previous, payload, at_utc, reversed)
        VALUES (?,?,?,?,?,?,?,0)`,
		actionID, string(u), original, applied, previous, string(payload),
		time.Now().UTC().Format(time.RFC3339Nano),
	)
	return err
}

// LiveUndos 返回所有尚未撤销的处置，用于进程重启后恢复状态、
// 以及在托盘里告诉用户「这些文件被处理过，可以还原」。
func (s *SQLiteStore) LiveUndos() ([]UndoRecord, error) {
	rows, err := s.db.Query(
		`SELECT action_id, kind, original, applied, previous, at_utc
           FROM undos WHERE reversed = 0 ORDER BY at_utc`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []UndoRecord
	for rows.Next() {
		var r UndoRecord
		if err := rows.Scan(&r.ActionID, &r.Kind, &r.Original, &r.Applied, &r.Previous, &r.AtUTC); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) MarkUndoReversed(actionID string) error {
	_, err := s.db.Exec(`UPDATE undos SET reversed = 1 WHERE action_id = ?`, actionID)
	return err
}

// UndoRecord 是落盘的撤销凭据。
type UndoRecord struct {
	ActionID string
	Kind     string
	Original string
	Applied  string
	Previous string
	AtUTC    string
}

var ErrNotFound = errors.New("store: 记录不存在")
