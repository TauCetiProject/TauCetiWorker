import Batteries.Tactic.Lint

/-! Worker-local diagnostics. Import only the selected modules and check only declarations
originating in those modules, including docstrings and nolint entries. Full-library coverage
and calibration belong to CI. Compiled once per toolchain/dependency identity. -/

open Lean Core Meta Batteries.Tactic.Lint

structure Finding where
  linter : String
  declaration : String
  detail : String
  deriving ToJson

/-- Match docBlame's declaration kinds, with legacy/private imports for doc visibility. -/
def documentable (name : Name) : MetaM Bool := do
  if (← getEnv).isPrivateOrAutoDecl name then return false
  if ← isInstance name then return false
  if let .str p _ := name then
    if ← isInstance p then return false
  if let .str _ s := name then
    if s == "parenthesizer" || s == "formatter" || s == "delaborator" || s == "quot" then
      return false
  match ← getConstInfo name with
  | .axiomInfo .. | .opaqueInfo .. | .inductInfo .. => return true
  | .defnInfo info => return !((← isProjectionFn name) && (← isProp info.type))
  | _ => return false

unsafe def main (args : List String) : IO UInt32 := do
  let [modulesFile, markerFile] := args
    | IO.eprintln "usage: local-lint <modules-file> <marker-file>"; return 2
  let modules := (← IO.FS.lines modulesFile).filter (!·.isEmpty) |>.map String.toName
  if modules.isEmpty then
    IO.eprintln "local-lint: no modules requested"; return 2
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let env ← importModules (modules.map ({ module := · })) {} (trustLevel := 1024)
    (loadExts := true)
  let ctx : Core.Context := { fileName := "local-lint", fileMap := default }
  let result ← Prod.fst <$> (CoreM.toIO · ctx { env }) do
    let idxs := modules.filterMap env.getModuleIdx?
    unless idxs.size == modules.size do throwError "requested lint module missing from environment"
    let decls := env.constants.fold (init := #[]) fun names name _ =>
      if (env.getModuleIdxFor? name).any idxs.contains then names.push name else names
    let mut findings : Array Finding := #[]
    let mut scanned := 0
    let mut documented := 0
    -- A stable imported Lean declaration calibrates doc visibility without importing any
    -- unrelated TauCeti module. Empty or theorem-only scopes need no documented declaration.
    unless (← findDocString? env `Lean.Expr).isSome do
      throwError "local-lint: imported docstrings unavailable (Lean.Expr calibration failed)"
    for name in decls do
      if ← MetaM.run' (documentable name) then
        scanned := scanned + 1
        if (← findDocString? env name).isSome then
          documented := documented + 1
        else
          findings := findings.push ⟨"docString", name.toString, "missing documentation string"⟩
    let mut nolints : Array (String × String) := #[]
    for idx in idxs do
      for (name, linters) in Batteries.Tactic.Lint.nolintAttr.ext.getModuleEntries env idx do
        for linter in linters do nolints := nolints.push (linter.toString, name.toString)
    let linters ← getChecks (slow := true) (runOnly := none) (runAlways := none)
    let linters := linters.filter (·.name != `docBlame)
    if linters.isEmpty then throwError "local-lint: no default environment linters registered"
    let results ← lintCore decls linters (inIO := true)
    for (linter, violations) in results do
      for (name, message) in violations do
        let detail ← (← addMessageContext message).toString
        findings := findings.push ⟨linter.name.toString, name.toString, detail⟩
    return Json.mkObj [
      ("modules", toJson modules.size), ("declarations", toJson decls.size),
      ("doc_scanned", toJson scanned), ("documented", toJson documented),
      ("linters", toJson linters.size), ("nolints", toJson nolints),
      ("findings", toJson findings)]
  IO.println s!"{(← IO.FS.readFile markerFile).trimAscii} {result.compress}"
  return 0
