import Lake.Build.Trace

/- Use the installed toolchain's hash, rather than duplicating Lake's hash algorithm in Python.
   This program reads files only: it never imports a candidate project or initializes Lake. -/
def main (args : List String) : IO UInt32 := do
  let [request] := args | return 2
  let paths : Array String ← match Lean.Json.parse (← IO.FS.readFile request) >>= Lean.fromJson? with
    | .ok paths => pure paths
    | .error e => throw (IO.userError e)
  for path in paths do
    let bytes ← IO.FS.readBinFile path
    IO.println (Lean.toJson (path, (Lake.Hash.ofByteArray bytes).val.toNat)).compress
  return 0
