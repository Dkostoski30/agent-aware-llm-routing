// Package main is the Endpoint Picker: llm-d-router v0.11.0's runner, as upstream's cmd/epp/main.go starts it (P3.1).
// Built here, so later tasks can register their plugins before Run, without a fork.
package main

import (
	"os"

	ctrl "sigs.k8s.io/controller-runtime"

	"github.com/llm-d/llm-d-router/cmd/epp/runner"
)

func main() {
	os.Exit(run())
}

// run returns the exit code. The runner has logged the error itself.
func run() int {
	ctx := ctrl.SetupSignalHandler()
	if err := runner.NewRunner().Run(ctx); err != nil {
		return 1
	}
	return 0
}
