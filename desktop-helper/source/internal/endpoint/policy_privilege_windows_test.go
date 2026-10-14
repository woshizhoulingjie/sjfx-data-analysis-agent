//go:build windows

package endpoint

import (
	"errors"
	"reflect"
	"testing"
	"unsafe"

	"golang.org/x/sys/windows"
)

func TestQueryAuditPolicyPrivilegeScope(t *testing.T) {
	queryFailure := errors.New("query failed")
	restoreFailure := errors.New("restore failed")
	acquireFailure := errors.New("privilege not assigned")
	for _, tc := range []struct {
		name       string
		acquireErr error
		queryErr   error
		restoreErr error
	}{
		{name: "success"},
		{name: "query failure", queryErr: queryFailure},
		{name: "restore failure", restoreErr: restoreFailure},
		{name: "query and restore failures", queryErr: queryFailure, restoreErr: restoreFailure},
		{name: "acquire failure", acquireErr: acquireFailure},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls []string
			flags, err := queryAuditPolicyScoped(func() (func() error, error) {
				calls = append(calls, "acquire")
				if tc.acquireErr != nil {
					return nil, tc.acquireErr
				}
				return func() error {
					calls = append(calls, "restore")
					return tc.restoreErr
				}, nil
			}, func() (uint32, error) {
				calls = append(calls, "query")
				return 3, tc.queryErr
			})
			wantCalls := []string{"acquire", "query", "restore"}
			wantFlags := uint32(3)
			if tc.acquireErr != nil {
				wantCalls = []string{"acquire"}
				wantFlags = 0
			}
			if flags != wantFlags || !reflect.DeepEqual(calls, wantCalls) {
				t.Fatalf("flags=%d calls=%v; want flags=%d calls=%v", flags, calls, wantFlags, wantCalls)
			}
			for _, expectedErr := range []error{tc.acquireErr, tc.queryErr, tc.restoreErr} {
				if expectedErr != nil && !errors.Is(err, expectedErr) {
					t.Errorf("error %v does not preserve %v", err, expectedErr)
				}
			}
			if tc.acquireErr == nil && tc.queryErr == nil && tc.restoreErr == nil && err != nil {
				t.Fatal(err)
			}
		})
	}
}

type securityPrivilegeState struct {
	assigned   bool
	attributes uint32
}

func currentSecurityPrivilegeState(t *testing.T) securityPrivilegeState {
	t.Helper()
	var token windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &token); err != nil {
		t.Fatal(err)
	}
	defer token.Close()
	name, err := windows.UTF16PtrFromString("SeSecurityPrivilege")
	if err != nil {
		t.Fatal(err)
	}
	var luid windows.LUID
	if err := windows.LookupPrivilegeValue(nil, name, &luid); err != nil {
		t.Fatal(err)
	}
	var length uint32
	err = windows.GetTokenInformation(token, windows.TokenPrivileges, nil, 0, &length)
	if !errors.Is(err, windows.ERROR_INSUFFICIENT_BUFFER) || length < uint32(unsafe.Sizeof(windows.Tokenprivileges{})) {
		t.Fatalf("TokenPrivileges size=%d error=%v", length, err)
	}
	buffer := make([]byte, length)
	if err := windows.GetTokenInformation(token, windows.TokenPrivileges, &buffer[0], length, &length); err != nil {
		t.Fatal(err)
	}
	privileges := (*windows.Tokenprivileges)(unsafe.Pointer(&buffer[0]))
	for _, privilege := range privileges.AllPrivileges() {
		if privilege.Luid == luid {
			return securityPrivilegeState{assigned: true, attributes: privilege.Attributes}
		}
	}
	return securityPrivilegeState{}
}

// This test only queries policy. It neither sets policy nor grants privileges.
// It covers both a filtered ordinary token and an elevated assigned token.
func TestGetAuditPolicyRestoresProcessPrivilege(t *testing.T) {
	before := currentSecurityPrivilegeState(t)
	flags, err := GetAuditPolicy()
	after := currentSecurityPrivilegeState(t)
	if after != before {
		t.Fatalf("SeSecurityPrivilege changed: before=%+v after=%+v", before, after)
	}
	if !before.assigned {
		if !errors.Is(err, windows.ERROR_NOT_ALL_ASSIGNED) {
			t.Fatalf("unassigned token: flags=%d error=%v; want ERROR_NOT_ALL_ASSIGNED", flags, err)
		}
		t.Log("ordinary token has no assigned SeSecurityPrivilege; query denied and token unchanged")
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if flags > 4 {
		t.Fatalf("unexpected audit policy flags %d", flags)
	}
	t.Logf("assigned SeSecurityPrivilege query succeeded: flags=%d original attributes=%d restored", flags, before.attributes)
}
