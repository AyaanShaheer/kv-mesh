// Command hashprobe prints the KV-Mesh fingerprint of each prompt read from
// standard input, one hexadecimal value per line.
//
// It exists so that the experiment harness can assert that the Python and Go
// implementations of the prompt fingerprint agree. The two implementations
// must agree exactly: the router hashes with one and the worker hashes with
// the other, and a disagreement would silently break cache affinity.
package main

import (
	"bufio"
	"fmt"
	"os"

	"kv-mesh/gateway/router"
)

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		fmt.Printf("%016x\n", router.HashPrompt(scanner.Text()))
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
