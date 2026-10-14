//go:build windows

package endpoint

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

const LocalTransactionFileLimit = 200000
const LocalTransactionBatchLimit = 500

func LocalTransactionByteLimit() int64 {
	value, err := strconv.ParseInt(os.Getenv("SJFX_LOCAL_AGENT_REGISTRATION_BYTES"), 10, 64)
	if err != nil || value < 1 || value > 1024*1024*1024*1024 {
		return 32 * 1024 * 1024 * 1024
	}
	return value
}

type LocalTransaction struct {
	Registration   LocalRegistration `json:"registration"`
	ExpectedCount  int               `json:"expected_count"`
	ExpectedBytes  int64             `json:"expected_bytes"`
	ManifestDigest string            `json:"manifest_digest"`
	Created        int64             `json:"created,omitempty"`
	NextOffset     int               `json:"next_offset,omitempty"`
	Committed      bool              `json:"committed,omitempty"`
}

// Shared language-neutral digest: ordered fields, NUL separator, newline row.
func LocalManifestDigest(files []LocalRegisterFile) (string, error) {
	hash := sha256.New()
	for _, f := range files {
		fields := []string{f.BindingID, f.FileID, f.Path, f.SHA256, f.VersionID, strconv.FormatInt(f.Size, 10), f.Identity}
		for _, field := range fields {
			if strings.ContainsAny(field, "\x00\r\n") {
				return "", fmt.Errorf("清单字段不能含NUL或换行")
			}
		}
		_, _ = hash.Write([]byte(strings.Join(fields, "\x00") + "\n"))
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func (r *Runtime) localRegistrationTransaction(ctx context.Context, path string, decoder *json.Decoder) (map[string]any, error) {
	if _, err := r.DB.DB.Exec("CREATE TABLE IF NOT EXISTS local_registration_parts(transaction_id TEXT NOT NULL,position INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(transaction_id,position))"); err != nil {
		return nil, err
	}
	if strings.HasSuffix(path, "/begin") {
		var input LocalTransaction
		if err := decoder.Decode(&input); err != nil {
			return nil, err
		}
		if len(input.Registration.Files) != 0 || input.Created != 0 || input.NextOffset != 0 || input.Committed {
			return nil, fmt.Errorf("begin仅接受登记元信息")
		}
		if input.ExpectedCount < 1 || input.ExpectedCount > LocalTransactionFileLimit || input.ExpectedBytes < 0 || input.ExpectedBytes > LocalTransactionByteLimit() {
			return nil, &LocalRegistrationCapacityError{"事务完整成员数或总hash容量超限"}
		}
		digest, err := hex.DecodeString(input.ManifestDigest)
		if err != nil || len(digest) != 32 {
			return nil, fmt.Errorf("清单摘要格式无效")
		}
		reg := input.Registration
		if reg.DeviceID == "" || reg.OwnerID == "" || reg.RegistrationID == "" || reg.ManifestRevision < 1 || reg.GapEpoch < 0 {
			return nil, fmt.Errorf("事务登记身份无效")
		}
		if _, err = selectedRoot(reg.LocalRoot); err != nil {
			return nil, err
		}
		if id := r.Client.GetIdentity().DeviceID; id != "" && id != reg.DeviceID {
			return nil, fmt.Errorf("设备已属于其他绑定")
		}
		var owner string
		_ = r.DB.LoadState("local_owner_id", &owner)
		if owner != "" && owner != reg.OwnerID {
			return nil, fmt.Errorf("账号已属于其他绑定")
		}
		var active int
		_ = r.DB.DB.QueryRow("SELECT COUNT(*) FROM state WHERE key LIKE 'local_tx:%'").Scan(&active)
		if active >= 8 {
			return nil, &LocalRegistrationCapacityError{"最多8个保留事务；先确认或取消旧事务"}
		}
		id := ID()
		input.Created = time.Now().Unix()
		if err = r.DB.SaveState("local_tx:"+id, input); err != nil {
			return nil, err
		}
		return map[string]any{"ok": true, "transaction_id": id, "next_offset": 0, "coverage_complete": false, "max_batch_files": LocalTransactionBatchLimit, "max_request_bytes": LocalAgentRequestLimit, "max_total_bytes": LocalTransactionByteLimit()}, nil
	}
	var input struct {
		TransactionID string              `json:"transaction_id"`
		Offset        int                 `json:"offset,omitempty"`
		Limit         int                 `json:"limit,omitempty"`
		Files         []LocalRegisterFile `json:"files,omitempty"`
	}
	if err := decoder.Decode(&input); err != nil {
		return nil, err
	}
	if len(input.TransactionID) > 128 || input.TransactionID == "" {
		return nil, fmt.Errorf("事务编号无效")
	}
	var transaction LocalTransaction
	if err := r.DB.LoadState("local_tx:"+input.TransactionID, &transaction); err != nil {
		return nil, err
	}
	if strings.HasSuffix(path, "/abort") || strings.HasSuffix(path, "/finish") {
		if strings.HasSuffix(path, "/abort") && transaction.Committed {
			return nil, fmt.Errorf("已提交事务需读取result；abort不能撤销正式登记")
		}
		if strings.HasSuffix(path, "/finish") && !transaction.Committed {
			return nil, fmt.Errorf("未提交事务不能确认完成")
		}
		tx, err := r.DB.DB.Begin()
		if err != nil {
			return nil, err
		}
		defer tx.Rollback()
		if _, err = tx.Exec("DELETE FROM local_registration_parts WHERE transaction_id=?", input.TransactionID); err != nil {
			return nil, err
		}
		if _, err = tx.Exec("DELETE FROM state WHERE key=?", "local_tx:"+input.TransactionID); err != nil {
			return nil, err
		}
		if err = tx.Commit(); err != nil {
			return nil, err
		}
		return map[string]any{"ok": true, "coverage_complete": transaction.Committed, "aborted": !transaction.Committed, "finished": transaction.Committed}, nil
	}
	if !transaction.Committed && time.Now().Unix()-transaction.Created > 86400 {
		return nil, fmt.Errorf("事务过期，必须取消后重新完整核验")
	}
	if strings.HasSuffix(path, "/batch") {
		if transaction.Committed || input.Offset < 0 || len(input.Files) == 0 || len(input.Files) > LocalTransactionBatchLimit || input.Offset+len(input.Files) > transaction.ExpectedCount {
			return nil, fmt.Errorf("批次超限或事务已提交")
		}
		if input.Offset != transaction.NextOffset {
			return nil, fmt.Errorf("必须从next_offset顺序分批，不接受缺失或重复成员")
		}
		if _, err := LocalManifestDigest(input.Files); err != nil {
			return nil, err
		}
		tx, err := r.DB.DB.Begin()
		if err != nil {
			return nil, err
		}
		defer tx.Rollback()
		for i, file := range input.Files {
			raw, _ := json.Marshal(file)
			if _, err = tx.Exec("INSERT INTO local_registration_parts VALUES(?,?,?)", input.TransactionID, input.Offset+i, string(raw)); err != nil {
				return nil, err
			}
		}
		transaction.NextOffset += len(input.Files)
		raw, _ := json.Marshal(transaction)
		if _, err = tx.Exec("UPDATE state SET value=? WHERE key=?", string(raw), "local_tx:"+input.TransactionID); err != nil {
			return nil, err
		}
		if err = tx.Commit(); err != nil {
			return nil, err
		}
		return map[string]any{"ok": true, "transaction_id": input.TransactionID, "next_offset": transaction.NextOffset, "coverage_complete": false}, nil
	}
	if strings.HasSuffix(path, "/commit") {
		if transaction.Committed {
			current := false
			for _, epoch := range r.Epochs() {
				if epoch.RegistrationID == transaction.Registration.RegistrationID && !epoch.Unresolved && epoch.GapEpoch == transaction.Registration.GapEpoch {
					current = true
				}
			}
			if !current {
				return nil, fmt.Errorf("提交回执已属于历史覆盖，当前缺口必须重新完整登记")
			}
		}
		if transaction.NextOffset != transaction.ExpectedCount {
			return nil, fmt.Errorf("完整清单尚未收齐，禁止提交部分覆盖")
		}
		if !transaction.Committed {
			rows, err := r.DB.DB.Query("SELECT payload FROM local_registration_parts WHERE transaction_id=? ORDER BY position", input.TransactionID)
			if err != nil {
				return nil, err
			}
			files := []LocalRegisterFile{}
			var total int64
			for rows.Next() {
				var raw string
				var file LocalRegisterFile
				if err = rows.Scan(&raw); err != nil {
					rows.Close()
					return nil, err
				}
				if err = json.Unmarshal([]byte(raw), &file); err != nil {
					rows.Close()
					return nil, err
				}
				if file.Size < 0 || total > LocalTransactionByteLimit()-file.Size {
					rows.Close()
					return nil, &LocalRegistrationCapacityError{"事务成员总容量超限"}
				}
				total += file.Size
				files = append(files, file)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return nil, err
			}
			digest, err := LocalManifestDigest(files)
			if err != nil {
				return nil, err
			}
			if len(files) != transaction.ExpectedCount || total != transaction.ExpectedBytes || digest != transaction.ManifestDigest {
				return nil, fmt.Errorf("完整成员数量、字节总数或摘要不一致")
			}
			reg := transaction.Registration
			reg.Files = files
			_, err = r.registerLocal(ctx, reg, LocalTransactionFileLimit, LocalTransactionByteLimit(), func(tx *sql.Tx, bindings []Binding) error {
				// Bindings, frozen receipts and complete marker activate in the SAME SQL commit.
				for i, b := range bindings {
					raw, _ := json.Marshal(b)
					if _, e := tx.Exec("UPDATE local_registration_parts SET payload=? WHERE transaction_id=? AND position=?", string(raw), input.TransactionID, i); e != nil {
						return e
					}
				}
				transaction.Committed = true
				raw, _ := json.Marshal(transaction)
				_, e := tx.Exec("UPDATE state SET value=? WHERE key=?", string(raw), "local_tx:"+input.TransactionID)
				return e
			})
			if err != nil {
				return nil, err
			}
		}
		return map[string]any{"ok": true, "coverage_complete": true, "transaction_id": input.TransactionID, "count": transaction.ExpectedCount, "manifest_digest": transaction.ManifestDigest, "gap_epoch": transaction.Registration.GapEpoch}, nil
	}
	if strings.HasSuffix(path, "/result") {
		if !transaction.Committed || input.Offset < 0 || input.Offset > transaction.ExpectedCount || input.Limit < 1 || input.Limit > 500 {
			return nil, fmt.Errorf("仅已提交事务可分页读取结果")
		}
		rows, err := r.DB.DB.Query("SELECT payload FROM local_registration_parts WHERE transaction_id=? AND position>=? ORDER BY position LIMIT ?", input.TransactionID, input.Offset, input.Limit)
		if err != nil {
			return nil, err
		}
		bindings := []Binding{}
		for rows.Next() {
			var raw string
			var b Binding
			if err = rows.Scan(&raw); err != nil {
				rows.Close()
				return nil, err
			}
			if err = json.Unmarshal([]byte(raw), &b); err != nil {
				rows.Close()
				return nil, err
			}
			bindings = append(bindings, b)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
		return map[string]any{"ok": true, "coverage_complete": true, "transaction_id": input.TransactionID, "offset": input.Offset, "next_offset": input.Offset + len(bindings), "count": transaction.ExpectedCount, "manifest_digest": transaction.ManifestDigest, "bindings": bindings}, nil
	}
	return nil, fmt.Errorf("事务接口不存在")
}
