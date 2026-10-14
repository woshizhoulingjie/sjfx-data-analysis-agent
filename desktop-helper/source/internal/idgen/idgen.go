// Package idgen 生成事件 ID。
//
// 用 ULID 风格（48 位毫秒时间戳 + 80 位随机数，Crockford Base32 编码），
// 不引入第三方依赖——标准库就够。
//
// 为什么不用时间戳（原来就是）：
//
//	同一毫秒内产生多条事件时，时间戳会撞。服务端按 event_id 去重，
//	撞了就等于**静默丢事件**，而且丢的是同一时刻的那一批——
//	恰恰是文件被批量操作时最需要留痕的时刻。
//
// 为什么不用纯随机 UUID：
//
//	ULID 前 48 位是时间，所以**按 ID 排序就是按时间排序**。
//	入队、出队、排查问题时不用额外按时间排序，直接按主键扫就行。
package idgen

import (
	"crypto/rand"
	"encoding/binary"
	"strings"
	"sync"
	"time"
)

// crockford 是 ULID 使用的 Base32 字母表（去掉了容易混淆的 I/L/O/U）。
const crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

// EncodedLen 是 ULID 的字符串长度。
const EncodedLen = 26

// 128 位要编成 26 个 Base32 字符：26*5 = 130 位，多出 2 位，
// 所以第一个字符只承载 3 位，其余 25 个字符各承载 5 位（3 + 125 = 128）。
const (
	firstCharBits = 3
	restCharBits  = 5
	timeChars     = 10 // 前 10 个字符编码 48 位时间戳
)

var (
	mu       sync.Mutex
	lastMs   int64
	lastRand [10]byte
)

// New 返回一个新的 ULID 字符串。
//
// 同一毫秒内多次调用**不会重复**：随机部分递增。
// 这不是"几乎不会重复"，是"不会重复"——两者的区别在批量场景下很重要。
func New() string {
	mu.Lock()
	defer mu.Unlock()
	return newLocked(time.Now().UnixMilli())
}

// NewAt 用指定时间生成 ID。测试用。
func NewAt(t time.Time) string {
	mu.Lock()
	defer mu.Unlock()
	return newLocked(t.UnixMilli())
}

func newLocked(ms int64) string {
	if ms == lastMs {
		increment(&lastRand)
	} else {
		lastMs = ms
		if _, err := rand.Read(lastRand[:]); err != nil {
			// crypto/rand 失败是极端情况（系统熵源异常）。
			// 退化成用纳秒填充，宁可碰撞概率上升也不能 panic 让代理挂掉。
			binary.BigEndian.PutUint64(lastRand[:8], uint64(time.Now().UnixNano()))
			lastRand[8], lastRand[9] = 0, 0
		}
	}
	return encode(ms, lastRand)
}

// increment 把 80 位随机部分当大端整数加一，溢出则回绕（概率可忽略）。
func increment(b *[10]byte) {
	for i := len(b) - 1; i >= 0; i-- {
		b[i]++
		if b[i] != 0 {
			return
		}
	}
}

// encode 把 48 位时间戳和 80 位随机数编成 26 个 Crockford Base32 字符。
func encode(ms int64, rnd [10]byte) string {
	var data [16]byte
	// 前 6 字节：48 位毫秒时间戳（大端）
	data[0] = byte(ms >> 40)
	data[1] = byte(ms >> 32)
	data[2] = byte(ms >> 24)
	data[3] = byte(ms >> 16)
	data[4] = byte(ms >> 8)
	data[5] = byte(ms)
	copy(data[6:], rnd[:])

	out := make([]byte, EncodedLen)
	out[0] = crockford[bits(data, 0, firstCharBits)]
	for i := 1; i < EncodedLen; i++ {
		start := firstCharBits + (i-1)*restCharBits
		out[i] = crockford[bits(data, start, restCharBits)]
	}
	return string(out)
}

// bits 从 128 位里取一段，start 从最高位开始数（0 起），n 是位数。
//
// 用逐位提取而不是位移运算，是因为 128 位跨了两个 uint64，
// 位移版本要处理一堆边界，容易写错且不好读。26 个字符总共只取 128 位，
// 性能完全不是问题。
func bits(data [16]byte, start, n int) byte {
	var v byte
	for i := 0; i < n; i++ {
		pos := start + i
		b := data[pos/8]
		bit := (b >> uint(7-pos%8)) & 1
		v = v<<1 | bit
	}
	return v
}

// TimeOf 从 ULID 里解出时间戳。用于排查"这条事件大概是什么时候的"。
func TimeOf(id string) (time.Time, bool) {
	if len(id) != EncodedLen {
		return time.Time{}, false
	}
	var ms int64
	for i := 0; i < timeChars; i++ {
		v := decodeChar(id[i])
		if v < 0 {
			return time.Time{}, false
		}
		ms = ms<<5 | int64(v)
	}
	return time.UnixMilli(ms), true
}

// decodeChar 把单个字符还原成 0-31。无法识别返回 -1。
func decodeChar(c byte) int {
	// 统一转大写
	if c >= 'a' && c <= 'z' {
		c -= 'a' - 'A'
	}
	// Crockford 允许把 I/L 当 1，O 当 0，方便人手抄写
	switch c {
	case 'I', 'L':
		return 1
	case 'O':
		return 0
	}
	return strings.IndexByte(crockford, c)
}
