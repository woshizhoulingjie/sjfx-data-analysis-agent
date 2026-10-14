//go:build windows

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/woshizhoulingjie/sjfx-agent/internal/endpoint"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"time"
)

func main() {
	base, err := os.UserConfigDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	server := flag.String("server", "", "SJFX 网站根地址，例如 https://sjfx.example.org")
	data := flag.String("data", filepath.Join(base, "SJFX", "Protection"), "助手状态目录")
	plain := flag.Bool("allow-http", false, "允许已信任内网使用 HTTP（推荐 HTTPS）")
	audit := flag.Bool("access-audit", false, "启用 Windows 访问审计，需要管理员启动并启用文件系统审计策略")
	restoreAudit := flag.Bool("restore-audit", false, "恢复本助手保存的文件审计权限后退出")
	checkUninstall := flag.Bool("check-uninstall", false, "检查是否存在尚未恢复的保护操作")
	localMode := flag.Bool("local", false, "独立本机模式：本机核心共享实例、版本及令牌，不配对远端")
	localToken := flag.String("local-token-file", "", "本机进程令牌文件；默认 SJFX_LOCAL_AGENT_TOKEN_FILE")
	listen := flag.String("listen", "127.0.0.1:18767", "独立本机进程接口端口，仅IPv4 loopback")
	stopFile := flag.String("stop-file", "", "所属控制器请求安全退出的文件")
	preflight := flag.Bool("audit-preflight", false, "只检查审计策略和Security读取权限，不改设置")
	configureAudit := flag.Bool("configure-access-audit", false, "配置已完整登记文件的SACL，不更改全局策略")
	restorePolicy := flag.Bool("restore-audit-policy", false, "安全恢复本安装器保存的全局审计策略")
	enableAudit := flag.Bool("enable-audit-policy", false, "备份并启用 Windows 文件系统成功/失败审计策略")
	flag.Parse()
	if gate := os.Getenv("SJFX_PROCESS_GATE"); gate != "" {
		deadline := time.Now().Add(30 * time.Second)
		for {
			raw, e := os.ReadFile(gate)
			if e == nil && string(raw) == os.Getenv("SJFX_PROCESS_TOKEN") {
				break
			}
			if time.Now().After(deadline) {
				fmt.Fprintln(os.Stderr, "控制器未取得助手进程归属")
				os.Exit(1)
			}
			time.Sleep(50 * time.Millisecond)
		}
	}
	dataExplicit := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == "data" {
			dataExplicit = true
		}
	})
	if *localMode && !dataExplicit {
		fmt.Fprintln(os.Stderr, "候选本机模式必须显式指定独立 --data 目录")
		os.Exit(1)
	}
	// A GUI subsystem executable has no console. Preserve startup/maintenance errors.
	if e := os.MkdirAll(*data, 0700); e == nil {
		logpath := filepath.Join(*data, "assistant.log")
		if st, e := os.Stat(logpath); e == nil && st.Size() > 5*1024*1024 {
			_ = os.Remove(logpath + ".previous")
			_ = os.Rename(logpath, logpath+".previous")
		}
		if log, e := os.OpenFile(logpath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); e == nil {
			os.Stderr = log
			defer log.Close()
		}
	}
	var r *endpoint.Runtime
	if *localMode {
		r, err = endpoint.NewLocalRuntime(*data, *localToken)
	} else {
		r, err = endpoint.NewRuntime(*server, *data, *plain)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *enableAudit {
		if err = endpoint.EnableAuditPolicy(*data); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	if *localMode {
		host, _, e := net.SplitHostPort(*listen)
		if e != nil || host != "127.0.0.1" {
			fmt.Fprintln(os.Stderr, "本机接口只能绑定127.0.0.1")
			os.Exit(1)
		}
		r.ListenAddr = *listen
	}
	if *preflight {
		report := endpoint.AuditPreflight(context.Background())
		_ = json.NewEncoder(os.Stdout).Encode(report)
		return
	}
	if *restorePolicy {
		if err = endpoint.RestoreAuditPolicy(*data); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	r.AccessAudit = *audit
	if *configureAudit {
		r.AccessAudit = true
		bindings, e := r.DB.Bindings()
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			os.Exit(1)
		}
		for {
			if err = r.ConfigureAccessAudit(context.Background(), bindings); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
			if r.Status()["access_audit_pending_count"].(int64) == 0 {
				break
			}
		}
		return
	}
	if *checkUninstall {
		journals, e := r.DB.Journals()
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			os.Exit(1)
		}
		for _, j := range journals {
			if j.Active || j.Intent {
				fmt.Fprintln(os.Stderr, "请先在文件保护页恢复处置", j.Action.ID)
				os.Exit(1)
			}
		}
		return
	}
	if *restoreAudit {
		if err = r.RestoreAccessAudit(context.Background()); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if *stopFile != "" {
		go func() {
			for {
				select {
				case <-ctx.Done():
					return
				case <-time.After(200 * time.Millisecond):
					if _, e := os.Stat(*stopFile); e == nil {
						cancel()
						return
					}
				}
			}
		}()
	}
	if err = r.Run(ctx); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
