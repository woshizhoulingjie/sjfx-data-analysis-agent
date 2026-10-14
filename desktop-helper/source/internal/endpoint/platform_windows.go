//go:build windows

package endpoint

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

func FileIdentity(path string) (string, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	h, err := windows.CreateFile(p, windows.FILE_READ_ATTRIBUTES, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_OPEN_REPARSE_POINT|windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(h)
	return HandleIdentity(h)
}
func HandleIdentity(h windows.Handle) (string, error) {
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(h, &info); err != nil {
		return "", err
	}
	if info.FileAttributes&(windows.FILE_ATTRIBUTE_REPARSE_POINT|windows.FILE_ATTRIBUTE_OFFLINE) != 0 {
		return "", fmt.Errorf("不跟随符号链接或云占位文件")
	}
	return fmt.Sprintf("%08x:%08x%08x", info.VolumeSerialNumber, info.FileIndexHigh, info.FileIndexLow), nil
}

func Snapshot(path string) (Binding, error) {
	return SnapshotContext(context.Background(), path)
}

type cancellableReader struct {
	context.Context
	io.Reader
}

func (c cancellableReader) Read(p []byte) (int, error) {
	if err := c.Context.Err(); err != nil {
		return 0, err
	}
	return c.Reader.Read(p)
}
func SnapshotContext(ctx context.Context, path string) (Binding, error) {
	identity, err := FileIdentity(path)
	if err != nil {
		return Binding{}, err
	}
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return Binding{}, err
	}
	h, err := windows.CreateFile(p, windows.GENERIC_READ, windows.FILE_SHARE_READ, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return Binding{}, err
	}
	f := os.NewFile(uintptr(h), path)
	defer f.Close()
	held, err := HandleIdentity(windows.Handle(f.Fd()))
	if err != nil || held != identity {
		return Binding{}, fmt.Errorf("打开时文件身份变化")
	}
	before, err := f.Stat()
	if err != nil {
		return Binding{}, err
	}
	if !before.Mode().IsRegular() {
		return Binding{}, fmt.Errorf("不是普通文件")
	}
	if before.Size() > MaxHashBytes {
		return Binding{}, fmt.Errorf("文件超出8GiB哈希核验容量")
	}
	sum := sha256.New()
	n, err := io.Copy(sum, io.LimitReader(cancellableReader{ctx, f}, MaxHashBytes+1))
	if err != nil {
		return Binding{}, err
	}
	if n > MaxHashBytes {
		return Binding{}, fmt.Errorf("读取期间超出8GiB核验容量")
	}
	after, err := f.Stat()
	if err != nil {
		return Binding{}, err
	}
	now, err := FileIdentity(path)
	if err != nil {
		return Binding{}, err
	}
	if before.Size() != after.Size() || before.ModTime() != after.ModTime() || now != identity {
		return Binding{}, fmt.Errorf("读取期间文件变化")
	}
	return Binding{Path: path, Identity: identity, SHA256: hex.EncodeToString(sum.Sum(nil)), Size: after.Size(), Mtime: after.ModTime().UnixNano(), State: "watching"}, nil
}

type blob struct {
	Size uint32
	Data *byte
}

var crypt32 = windows.NewLazySystemDLL("crypt32.dll")
var kernel32 = windows.NewLazySystemDLL("kernel32.dll")

func protectSecret(data []byte, decrypt bool) ([]byte, error) {
	if len(data) == 0 {
		return nil, fmt.Errorf("empty secret")
	}
	input := blob{uint32(len(data)), &data[0]}
	var output blob
	name := "CryptProtectData"
	if decrypt {
		name = "CryptUnprotectData"
	}
	r, _, err := crypt32.NewProc(name).Call(uintptr(unsafe.Pointer(&input)), 0, 0, 0, 0, 1, uintptr(unsafe.Pointer(&output)))
	if r == 0 {
		return nil, err
	}
	defer kernel32.NewProc("LocalFree").Call(uintptr(unsafe.Pointer(output.Data)))
	return append([]byte(nil), unsafe.Slice(output.Data, output.Size)...), nil
}
func SaveIdentity(path string, id Identity) error {
	raw, err := json.Marshal(id)
	if err != nil {
		return err
	}
	raw, err = protectSecret(raw, false)
	if err != nil {
		return err
	}
	temp := path + ".tmp"
	if err = os.WriteFile(temp, raw, 0600); err != nil {
		return err
	}
	return os.Rename(temp, path)
}
func LoadIdentity(path string) (Identity, error) {
	var id Identity
	raw, err := os.ReadFile(path)
	if err != nil {
		return id, err
	}
	raw, err = protectSecret(raw, true)
	if err != nil {
		return id, err
	}
	err = json.Unmarshal(raw, &id)
	return id, err
}

func ChooseDirectory() (string, error) {
	// Constant script: no server/user text is interpolated into PowerShell.
	script := `Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='选择需要关联并监视的本地资料目录'; $d.ShowNewFolderButton=$false; if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();[Console]::Write($d.SelectedPath)}`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-STA", "-Command", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	path := strings.TrimSpace(string(out))
	if path == "" {
		return "", fmt.Errorf("已取消目录选择")
	}
	return filepath.Abs(path)
}

// Held handles deny operations before they complete, scoped to one file.
// delete protection also blocks replace-by-rename saves; expose this explicitly in UI.
func HoldProtection(path, kind string) (windows.Handle, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	share := uint32(windows.FILE_SHARE_READ | windows.FILE_SHARE_WRITE)
	access := uint32(windows.GENERIC_READ)
	if kind == "quarantine" {
		access |= windows.DELETE
	}
	if kind == "deny_read" || kind == "quarantine" {
		share = 0
	}
	return windows.CreateFile(p, access, share, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
}

// Rename the held object. No subsequent path lookup can substitute another source.
// ReplaceIfExists is deliberately zero; restoring never overwrites a new instance.
func RenameHeld(h windows.Handle, destination string) error {
	abs, err := filepath.Abs(destination)
	if err != nil {
		return err
	}
	name, err := windows.UTF16FromString(abs)
	if err != nil {
		return err
	}
	type renameInfo struct {
		Replace uint32
		Root    windows.Handle
		Length  uint32
		Name    [1]uint16
	}
	var layout renameInfo
	offset := int(unsafe.Offsetof(layout.Name))
	buf := make([]byte, offset+len(name)*2)
	info := (*renameInfo)(unsafe.Pointer(&buf[0]))
	// Keep a terminating UTF-16 NUL in the allocation. FileNameLength excludes
	// it, but Windows' DOS-path conversion may inspect the terminator.
	info.Length = uint32((len(name) - 1) * 2)
	copy(unsafe.Slice((*uint16)(unsafe.Pointer(&buf[offset])), len(name)), name)
	return windows.SetFileInformationByHandle(h, windows.FileRenameInfo, &buf[0], uint32(len(buf)))
}
func CloseProtection(h uintptr) error { return windows.CloseHandle(windows.Handle(h)) }
func HashHandle(h windows.Handle) (string, error) {
	if _, err := windows.Seek(h, 0, io.SeekStart); err != nil {
		return "", err
	}
	hash := sha256.New()
	buf := make([]byte, 1024*1024)
	var total int64
	for {
		var n uint32
		err := windows.ReadFile(h, buf, &n, nil)
		if n > 0 {
			total += int64(n)
			if total > MaxHashBytes {
				return "", fmt.Errorf("文件超过哈希容量")
			}
			hash.Write(buf[:n])
		}
		if err == windows.ERROR_HANDLE_EOF || n == 0 && err == nil {
			break
		}
		if err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
