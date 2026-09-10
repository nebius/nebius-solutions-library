package executor

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

var restoreNamespaceNames = []string{"uts", "ipc", "net", "pid"}

type pinnedRestoreNamespaces struct {
	files []*os.File
}

func openPinnedRestoreNamespaces(procRoot string, pid int, pinnedMount *os.File) (*pinnedRestoreNamespaces, error) {
	if pid <= 0 || pinnedMount == nil {
		return nil, fmt.Errorf("restore namespace pinning requires a live PID and mount namespace fd")
	}
	startBefore, err := readProcessStartTime(procRoot, pid)
	if err != nil {
		return nil, err
	}
	currentMount, err := os.Open(filepath.Join(procRoot, strconv.Itoa(pid), "ns", "mnt"))
	if err != nil {
		return nil, fmt.Errorf("open current mount namespace: %w", err)
	}
	currentMountInfo, err := currentMount.Stat()
	currentMount.Close()
	if err != nil {
		return nil, fmt.Errorf("stat current mount namespace: %w", err)
	}
	pinnedMountInfo, err := pinnedMount.Stat()
	if err != nil {
		return nil, fmt.Errorf("stat pinned mount namespace: %w", err)
	}
	if !os.SameFile(currentMountInfo, pinnedMountInfo) {
		return nil, fmt.Errorf("pinned mount namespace no longer belongs to restore PID %d", pid)
	}

	pinned := &pinnedRestoreNamespaces{}
	for _, name := range restoreNamespaceNames {
		file, err := os.Open(filepath.Join(procRoot, strconv.Itoa(pid), "ns", name))
		if err != nil {
			pinned.close()
			return nil, fmt.Errorf("open %s namespace: %w", name, err)
		}
		pinned.files = append(pinned.files, file)
	}
	startAfter, err := readProcessStartTime(procRoot, pid)
	if err != nil {
		pinned.close()
		return nil, err
	}
	if startBefore != startAfter {
		pinned.close()
		return nil, fmt.Errorf("restore PID changed while namespaces were pinned")
	}
	return pinned, nil
}

func (p *pinnedRestoreNamespaces) close() {
	if p == nil {
		return
	}
	for _, file := range p.files {
		_ = file.Close()
	}
	p.files = nil
}

func (p *pinnedRestoreNamespaces) nsenterArgs(firstChildFD int) []string {
	args := make([]string, 0, len(restoreNamespaceNames))
	for index, name := range restoreNamespaceNames {
		args = append(args, fmt.Sprintf("--%s=/proc/self/fd/%d", name, firstChildFD+index))
	}
	return args
}

func readProcessStartTime(procRoot string, pid int) (string, error) {
	data, err := os.ReadFile(filepath.Join(procRoot, strconv.Itoa(pid), "stat"))
	if err != nil {
		return "", fmt.Errorf("read restore PID stat: %w", err)
	}
	// /proc/<pid>/stat field 2 is parenthesized and may contain spaces or ')'.
	// The last ')' terminates it; starttime is field 22, index 19 in the suffix
	// that begins with field 3 (state).
	closeIndex := strings.LastIndexByte(string(data), ')')
	if closeIndex < 0 {
		return "", fmt.Errorf("restore PID stat is malformed")
	}
	fields := strings.Fields(string(data[closeIndex+1:]))
	if len(fields) <= 19 || fields[19] == "" {
		return "", fmt.Errorf("restore PID stat omits starttime")
	}
	return fields[19], nil
}
