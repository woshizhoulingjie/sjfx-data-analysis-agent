package identity

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

// ---------------------------------------------------------------------------
// FileStore：把身份存成一个 JSON 文件
//
// 注意这只是「能跑」的实现，安全强度取决于目录权限：
// Windows 下应放在 %ProgramData%\sjfx\ 并收紧 ACL，
// macOS 应该改用 Keychain，Linux 用 0600 且属主为 root。
//
// 生产环境要按平台替换——凭据文件被普通用户读到，
// 等于 node key 泄露，攻击者可以伪造这台设备上报假事件。
// ---------------------------------------------------------------------------

type FileStore struct {
	path string
}

func NewFileStore(dir string) *FileStore {
	return &FileStore{path: filepath.Join(dir, "identity.json")}
}

func (s *FileStore) Load() (*Identity, error) {
	b, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotEnrolled
	}
	if err != nil {
		return nil, err
	}
	var id Identity
	if err := json.Unmarshal(b, &id); err != nil {
		return nil, err
	}
	if id.NodeKey == "" {
		return nil, ErrNotEnrolled
	}
	return &id, nil
}

func (s *FileStore) Save(id *Identity) error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(id, "", "  ")
	if err != nil {
		return err
	}
	// 0600：只有属主可读写
	return os.WriteFile(s.path, b, 0o600)
}

func (s *FileStore) Clear() error {
	err := os.Remove(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// ---------------------------------------------------------------------------
// FileSecret：安装时写入的一次性注册密钥
// ---------------------------------------------------------------------------

type FileSecret struct {
	path string
}

func NewFileSecret(path string) *FileSecret { return &FileSecret{path: path} }

func (s *FileSecret) Read() (string, error) {
	b, err := os.ReadFile(s.path)
	if err != nil {
		return "", err
	}
	return string(trimSpace(b)), nil
}

// Erase 在注册成功后把密钥从磁盘抹掉。
//
// 为什么必须删：enroll secret 是可以无限次注册新设备的凭据。
// 留在磁盘上，任何能读到它的程序都能拿它注册一台假设备进系统。
func (s *FileSecret) Erase() error {
	err := os.Remove(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func trimSpace(b []byte) []byte {
	start, end := 0, len(b)
	for start < end && isSpace(b[start]) {
		start++
	}
	for end > start && isSpace(b[end-1]) {
		end--
	}
	return b[start:end]
}

func isSpace(c byte) bool { return c == ' ' || c == '\n' || c == '\r' || c == '\t' }
