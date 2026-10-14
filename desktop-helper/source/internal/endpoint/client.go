package endpoint

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Identity struct {
	DeviceID   string `json:"device_id"`
	PrivateKey []byte `json:"private_key"`
}
type Client struct {
	mu       sync.RWMutex
	BaseURL  string
	Identity Identity
	HTTP     *http.Client
}

func (c *Client) GetIdentity() Identity   { c.mu.RLock(); defer c.mu.RUnlock(); return c.Identity }
func (c *Client) SetIdentity(id Identity) { c.mu.Lock(); defer c.mu.Unlock(); c.Identity = id }

func ID() string {
	b := make([]byte, 16)
	_, err := rand.Read(b)
	if err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
func Hash(data []byte) string { s := sha256.Sum256(data); return hex.EncodeToString(s[:]) }
func (c *Client) Proof(prefix string) map[string]string {
	id := c.GetIdentity()
	stamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := ID()
	message := strings.Join([]string{prefix, id.DeviceID, stamp, nonce}, "\n")
	return map[string]string{"X-SJFX-Device": id.DeviceID, "X-SJFX-Time": stamp, "X-SJFX-Nonce": nonce,
		"X-SJFX-Proof": base64.StdEncoding.EncodeToString(ed25519.Sign(id.PrivateKey, []byte(message)))}
}
func (c *Client) Post(ctx context.Context, path string, input, output any, anonymous bool) error {
	body, err := json.Marshal(input)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if !anonymous {
		id := c.GetIdentity()
		if len(id.PrivateKey) != ed25519.PrivateKeySize || id.DeviceID == "" {
			return fmt.Errorf("请先绑定设备")
		}
		for k, v := range c.Proof(strings.Join([]string{"sjfx-agent-v2", "POST", path, Hash(body)}, "\n")) {
			req.Header.Set(k, v)
		}
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8*1024*1024))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var v struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &v)
		return fmt.Errorf("服务器拒绝(%d)：%s", resp.StatusCode, v.Error)
	}
	if output == nil {
		return nil
	}
	return json.Unmarshal(raw, output)
}
