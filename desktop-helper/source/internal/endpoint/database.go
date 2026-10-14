package endpoint

import (
	"database/sql"
	"encoding/json"
	"fmt"
	_ "modernc.org/sqlite"
	"os"
	"path/filepath"
	"time"
)

type Binding struct {
	ID                   string `json:"binding_id"`
	FileID               string `json:"file_id"`
	Path                 string `json:"local_path"`
	Identity             string `json:"local_identity"`
	SHA256               string `json:"sha256"`
	Size                 int64  `json:"size"`
	Mtime                int64  `json:"mtime"`
	State                string `json:"state"`
	RegistrationID       string `json:"registration_id"`
	RegistrationRevision int64  `json:"registration_revision"`
	ManifestGeneration   int64  `json:"manifest_generation"`
	GapEpoch             int64  `json:"gap_epoch"`
	VersionID            string `json:"version_id"`
	DeviceID             string `json:"device_id"`
	LocalRoot            string `json:"local_root"`
	Disposition          string `json:"disposition,omitempty"`
	OwnerID              string `json:"owner_id,omitempty"`
}
type Event struct {
	EventID         string  `json:"event_id"`
	BindingID       string  `json:"binding_id,omitempty"`
	Action          string  `json:"action"`
	Path            string  `json:"path,omitempty"`
	OldPath         string  `json:"old_path,omitempty"`
	Identity        string  `json:"identity,omitempty"`
	SHA256          string  `json:"sha256,omitempty"`
	Occurred        float64 `json:"occurred"`
	Detail          string  `json:"detail,omitempty"`
	Process         string  `json:"process,omitempty"`
	Sequence        int64   `json:"sequence"`
	RegistrationID  string  `json:"registration_id,omitempty"`
	LocalRoot       string  `json:"local_root,omitempty"`
	GapEpoch        int64   `json:"gap_epoch"`
	VersionID       string  `json:"version_id,omitempty"`
	Size            int64   `json:"size,omitempty"`
	DeviceID        string  `json:"device_id,omitempty"`
	OwnerID         string  `json:"owner_id,omitempty"`
	User            string  `json:"user,omitempty"`
	ProcessID       string  `json:"process_id,omitempty"`
	VersionEvidence string  `json:"version_evidence,omitempty"`
	AccessMask      string  `json:"access_mask,omitempty"`
	NativeEventID   int     `json:"native_event_id,omitempty"`
	NativeRecordID  int64   `json:"native_record_id,omitempty"`
}

const DefaultEventLimit = 50000
const DefaultEventBytes int64 = 64 * 1024 * 1024
const MaxEventPayloadBytes = 256 * 1024
const DefaultCandidateLimit = 20000

type Database struct {
	DB             *sql.DB
	EventLimit     int
	EventBytes     int64
	CandidateLimit int
}
type QueueCapacityError struct{ Detail string }

func (e *QueueCapacityError) Error() string { return "持久事件暂存容量不足：" + e.Detail }

func (d *Database) checkEventCapacity(tx *sql.Tx, bytes int) error {
	var count, total int64
	if err := tx.QueryRow("SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM events").Scan(&count, &total); err != nil {
		return err
	}
	if bytes > MaxEventPayloadBytes || count >= int64(d.EventLimit) || total+int64(bytes) > d.EventBytes {
		return &QueueCapacityError{fmt.Sprintf("events=%d bytes=%d incoming=%d", count, total, bytes)}
	}
	return nil
}

func (d *Database) noteCapacity(err error) {
	if _, ok := err.(*QueueCapacityError); !ok {
		return
	}
	// Persist loss evidence outside the rolled back event transaction, even if
	// the queue is full. ACK does not erase this evidence or registration gaps.
	_, _ = d.DB.Exec("INSERT INTO state VALUES('event_capacity_rejections','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
}

// Persist a change and its outbox event together, including after a crash.
func (d *Database) Record(b Binding, e Event) (outErr error) {
	defer func() { d.noteCapacity(outErr) }()
	if e.EventID == "" {
		e.EventID = ID()
	}
	if e.Occurred == 0 {
		e.Occurred = float64(time.Now().UnixMilli()) / 1000
	}
	raw, err := json.Marshal(b)
	if err != nil {
		return err
	}
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	e.RegistrationID, e.GapEpoch, e.VersionID, e.Size = b.RegistrationID, b.GapEpoch, b.VersionID, b.Size
	e.DeviceID, e.OwnerID = b.DeviceID, b.OwnerID
	if err = nextSequence(tx, &e); err != nil {
		return err
	}
	event, err := json.Marshal(e)
	if err != nil {
		return err
	}
	if err = d.checkEventCapacity(tx, len(event)); err != nil {
		return err
	}
	if _, err = tx.Exec("INSERT INTO bindings VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", b.ID, string(raw)); err != nil {
		return err
	}
	if _, err = tx.Exec("INSERT OR IGNORE INTO versions VALUES(?,?)", b.ID, b.SHA256); err != nil {
		return err
	}
	if _, err = tx.Exec("INSERT OR IGNORE INTO events VALUES(?,?,?)", e.EventID, string(event), e.Occurred); err != nil {
		return err
	}
	return tx.Commit()
}
func OpenDatabase(dir string) (*Database, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", filepath.Join(dir, "endpoint.db")+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY);
 CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS versions(binding_id TEXT,sha256 TEXT,PRIMARY KEY(binding_id,sha256));
 CREATE TABLE IF NOT EXISTS candidates(path TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,last_seen REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS local_results(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Database{DB: db, EventLimit: DefaultEventLimit, EventBytes: DefaultEventBytes, CandidateLimit: DefaultCandidateLimit}, nil
}
func (d *Database) PutBinding(b Binding) error {
	raw, _ := json.Marshal(b)
	_, err := d.DB.Exec("INSERT INTO bindings VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", b.ID, string(raw))
	if err != nil {
		return err
	}
	_, err = d.DB.Exec("INSERT OR IGNORE INTO versions VALUES(?,?)", b.ID, b.SHA256)
	return err
}
func (d *Database) Bindings() ([]Binding, error) {
	rows, err := d.DB.Query("SELECT payload FROM bindings")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Binding
	for rows.Next() {
		var raw string
		if err = rows.Scan(&raw); err != nil {
			return nil, err
		}
		var b Binding
		if err = json.Unmarshal([]byte(raw), &b); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}
func (d *Database) AddRoot(path string) error {
	_, err := d.DB.Exec("INSERT OR IGNORE INTO roots VALUES(?)", path)
	return err
}
func (d *Database) Roots() ([]string, error) {
	rows, err := d.DB.Query("SELECT path FROM roots")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err = rows.Scan(&s); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}
func (d *Database) Enqueue(e Event) (outErr error) {
	defer func() { d.noteCapacity(outErr) }()
	if e.EventID == "" {
		e.EventID = ID()
	}
	if e.Occurred == 0 {
		e.Occurred = float64(time.Now().UnixMilli()) / 1000
	}
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var exists int
	if err = tx.QueryRow("SELECT COUNT(*) FROM events WHERE id=?", e.EventID).Scan(&exists); err != nil {
		return err
	}
	if exists != 0 {
		return nil
	}
	if err = nextSequence(tx, &e); err != nil {
		return err
	}
	raw, err := json.Marshal(e)
	if err != nil {
		return err
	}
	if err = d.checkEventCapacity(tx, len(raw)); err != nil {
		return err
	}
	_, err = tx.Exec("INSERT OR IGNORE INTO events VALUES(?,?,?)", e.EventID, string(raw), e.Occurred)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// Candidate identity and content are durable independently of event ACKs.
// One fingerprint transition emits one event; restart and repeated scans do not.
func (d *Database) EnqueueCandidate(key, fingerprint string, e Event) (outErr error) {
	defer func() { d.noteCapacity(outErr) }()
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var previous string
	err = tx.QueryRow("SELECT fingerprint FROM candidates WHERE path=?", key).Scan(&previous)
	if err != nil && err != sql.ErrNoRows {
		return err
	}
	if err == sql.ErrNoRows {
		var count int
		if err = tx.QueryRow("SELECT COUNT(*) FROM candidates").Scan(&count); err != nil {
			return err
		}
		if count >= d.CandidateLimit {
			return &QueueCapacityError{"候选实例目录容量达到上限"}
		}
	}
	stamp := float64(time.Now().UnixMilli()) / 1000
	if previous != fingerprint {
		e.EventID, e.Occurred = ID(), stamp
		if err = nextSequence(tx, &e); err != nil {
			return err
		}
		raw, err := json.Marshal(e)
		if err != nil {
			return err
		}
		if err = d.checkEventCapacity(tx, len(raw)); err != nil {
			return err
		}
		if _, err = tx.Exec("INSERT INTO events VALUES(?,?,?)", e.EventID, string(raw), stamp); err != nil {
			return err
		}
	}
	if _, err = tx.Exec("INSERT INTO candidates VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint,last_seen=excluded.last_seen", key, fingerprint, stamp); err != nil {
		return err
	}
	return tx.Commit()
}

func (d *Database) ForgetAbsentCandidates(present map[string]bool) error {
	rows, err := d.DB.Query("SELECT path FROM candidates")
	if err != nil {
		return err
	}
	var absent []string
	for rows.Next() {
		var path string
		if err = rows.Scan(&path); err != nil {
			rows.Close()
			return err
		}
		if !present[path] {
			absent = append(absent, path)
		}
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, path := range absent {
		if _, err = tx.Exec("DELETE FROM candidates WHERE path=?", path); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func nextSequence(tx *sql.Tx, e *Event) error {
	var old int64
	if err := tx.QueryRow("SELECT CAST(value AS INTEGER) FROM state WHERE key='device_sequence'").Scan(&old); err != nil && err != sql.ErrNoRows {
		return err
	}
	e.Sequence = old + 1
	_, err := tx.Exec("INSERT INTO state VALUES('device_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", e.Sequence)
	return err
}

func (d *Database) SaveState(key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = d.DB.Exec("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, string(raw))
	return err
}
func (d *Database) LoadState(key string, value any) error {
	var raw string
	if err := d.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&raw); err != nil {
		return err
	}
	return json.Unmarshal([]byte(raw), value)
}
func (d *Database) OldestEventAge() float64 {
	var stamp sql.NullFloat64
	_ = d.DB.QueryRow("SELECT MIN(created) FROM events").Scan(&stamp)
	if !stamp.Valid {
		return 0
	}
	return float64(time.Now().UnixMilli())/1000 - stamp.Float64
}
func (d *Database) Pending() ([]Event, error) {
	rows, err := d.DB.Query("SELECT payload FROM events ORDER BY rowid LIMIT 100")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var s string
		if err = rows.Scan(&s); err != nil {
			return nil, err
		}
		var e Event
		if err = json.Unmarshal([]byte(s), &e); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}
func (d *Database) Ack(ids []string) error {
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, id := range ids {
		if _, err = tx.Exec("DELETE FROM events WHERE id=?", id); err != nil {
			return err
		}
	}
	return tx.Commit()
}
func (d *Database) Depth() int {
	var n int
	_ = d.DB.QueryRow("SELECT COUNT(*) FROM events").Scan(&n)
	return n
}
func (d *Database) SaveAction(id string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = d.DB.Exec("INSERT INTO actions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", id, string(raw))
	return err
}
func (d *Database) LoadAction(id string, value any) error {
	var raw string
	if err := d.DB.QueryRow("SELECT payload FROM actions WHERE id=?", id).Scan(&raw); err != nil {
		return err
	}
	return json.Unmarshal([]byte(raw), value)
}
