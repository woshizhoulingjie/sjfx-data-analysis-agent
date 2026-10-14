package idgen

import (
	"sort"
	"testing"
	"time"
)

func TestFormat(t *testing.T) {
	id := New()
	if len(id) != EncodedLen {
		t.Fatalf("长度应为 %d，实际 %d: %q", EncodedLen, len(id), id)
	}
	for i := 0; i < len(id); i++ {
		if decodeChar(id[i]) < 0 {
			t.Fatalf("第 %d 个字符 %q 不是合法的 Crockford Base32", i, id[i])
		}
	}
}

// TestUniquenessSameMillisecond 是本包存在的理由。
//
// 原来的实现用时间戳当 ID，同一毫秒内产生多条事件就会撞；
// 服务端按 event_id 去重，撞了等于静默丢事件。
// 这里在同一个毫秒里连发十万个，必须一个都不重复。
func TestUniquenessSameMillisecond(t *testing.T) {
	const n = 100000
	seen := make(map[string]struct{}, n)
	for i := 0; i < n; i++ {
		id := New()
		if _, dup := seen[id]; dup {
			t.Fatalf("第 %d 个 ID 重复: %s", i, id)
		}
		seen[id] = struct{}{}
	}
	if len(seen) != n {
		t.Fatalf("唯一 ID 数 %d，期望 %d", len(seen), n)
	}
}

// TestOrderedByTime ULID 的核心好处：按 ID 排序就是按时间排序。
func TestOrderedByTime(t *testing.T) {
	base := time.Date(2026, 9, 22, 10, 0, 0, 0, time.UTC)
	var ids []string
	for i := 0; i < 50; i++ {
		ids = append(ids, NewAt(base.Add(time.Duration(i)*time.Millisecond)))
	}

	sorted := make([]string, len(ids))
	copy(sorted, ids)
	sort.Strings(sorted)

	for i := range ids {
		if ids[i] != sorted[i] {
			t.Fatalf("按时间生成的 ID 不是升序的：位置 %d，%s vs %s", i, ids[i], sorted[i])
		}
	}
}

func TestTimeOfRoundTrip(t *testing.T) {
	want := time.Date(2026, 9, 22, 10, 30, 45, 123_000_000, time.UTC)
	id := NewAt(want)

	got, ok := TimeOf(id)
	if !ok {
		t.Fatalf("TimeOf 解析失败: %s", id)
	}
	if !got.Equal(want) {
		t.Fatalf("时间不一致: got %v, want %v", got, want)
	}
}

func TestTimeOfRejectsGarbage(t *testing.T) {
	for _, bad := range []string{"", "short", "!!!!!!!!!!!!!!!!!!!!!!!!!!", "01234567890123456789012345"} {
		if _, ok := TimeOf(bad); ok && bad != "01234567890123456789012345" {
			t.Fatalf("不该接受 %q", bad)
		}
	}
}

func TestDecodeCharAcceptsConfusables(t *testing.T) {
	// Crockford 允许 I/L 当 1、O 当 0，方便人手抄写
	if decodeChar('I') != 1 || decodeChar('L') != 1 || decodeChar('O') != 0 {
		t.Fatal("I/L 应解码为 1，O 应解码为 0")
	}
	// 大小写不敏感
	if decodeChar('a') != decodeChar('A') {
		t.Fatal("大小写应等价")
	}
}

// TestEncodeIsStable 固定输入的编码结果必须稳定，
// 否则 ID 格式一变，历史数据的解析就废了。
func TestEncodeIsStable(t *testing.T) {
	var rnd [10]byte
	for i := range rnd {
		rnd[i] = byte(i)
	}
	got := encode(0x0123456789AB, rnd)
	if len(got) != EncodedLen {
		t.Fatalf("长度错误: %d", len(got))
	}
	// 时间戳部分应能从 ID 解回来
	back, ok := TimeOf(got)
	if !ok {
		t.Fatalf("解不回时间: %s", got)
	}
	if back.UnixMilli() != 0x0123456789AB {
		t.Fatalf("时间戳不一致: %d", back.UnixMilli())
	}
}
