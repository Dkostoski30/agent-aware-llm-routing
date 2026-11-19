package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const emptyPod = "vllm:num_requests_running{model_name=\"base\"} 0\nvllm:num_requests_waiting{model_name=\"base\"} 0\n"

func TestAWrongCommandLineExitsTwo(t *testing.T) {
	for name, args := range map[string][]string{
		"nothing":            {},
		"no such command":    {"drain"},
		"install, no path":   {"install"},
		"install, two paths": {"install", "a", "b"},
		"wait, no metrics":   {"wait", "--timeout=120s", "--quiet=5s"},
		"wait, no timeout":   {"wait", "--metrics=http://127.0.0.1:8000/metrics", "--quiet=5s"},
		"wait, no quiet":     {"wait", "--metrics=http://127.0.0.1:8000/metrics", "--timeout=120s"},
		"wait, zero timeout": {"wait", "--metrics=http://127.0.0.1:8000/metrics", "--timeout=0s", "--quiet=5s"},
		"wait, a stray word": {"wait", "--metrics=http://127.0.0.1:8000/metrics", "--timeout=1s", "--quiet=0s", "x"},
		"wait, no duration":  {"wait", "--metrics=http://127.0.0.1:8000/metrics", "--timeout=120", "--quiet=5s"},
	} {
		var stderr bytes.Buffer
		if code := run(args, &stderr); code != 2 {
			t.Errorf("%s: exit %d, want 2", name, code)
		}
	}
}

func TestInstallCopiesTheBinaryAsAnExecutable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "drainwait")
	var stderr bytes.Buffer
	if code := run([]string{"install", path}, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatal("the copy differs from the binary")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o755 {
		t.Fatalf("mode %v, want 0755", info.Mode().Perm())
	}
}

func TestInstallIntoAMissingDirectoryExitsTwo(t *testing.T) {
	var stderr bytes.Buffer
	if code := run([]string{"install", filepath.Join(t.TempDir(), "missing", "drainwait")}, &stderr); code != 2 {
		t.Fatalf("exit %d, want 2", code)
	}
}

func TestWaitEndsIdleOnAnEmptyPod(t *testing.T) {
	pod := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(emptyPod))
	}))
	defer pod.Close()
	var stderr bytes.Buffer
	if code := run([]string{"wait", "--metrics=" + pod.URL, "--timeout=10s", "--quiet=0s"}, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "drainwait: idle") {
		t.Fatalf("printed %q, want idle", stderr.String())
	}
}

func TestWaitCountsAnErrorStatusAsBusy(t *testing.T) {
	// The body holds empty counts, but a 503 is no reading of them: the drain runs to its timeout.
	pod := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(emptyPod))
	}))
	defer pod.Close()
	var stderr bytes.Buffer
	began := time.Now()
	if code := run([]string{"wait", "--metrics=" + pod.URL, "--timeout=1s", "--quiet=0s"}, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "drainwait: timeout") || !strings.Contains(stderr.String(), "503") {
		t.Fatalf("printed %q, want a timeout with the 503", stderr.String())
	}
	if time.Since(began) < time.Second {
		t.Fatal("ended before its timeout")
	}
}
