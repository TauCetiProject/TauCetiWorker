import Lean.Environment
import Lean.Util.FoldConsts
import Lean.Util.Path

/-!
Fast LOCAL diagnostics, not the authoritative transitive/global CI audit.

Read only the selected modules' own compiled declarations, including private proofs.
Never construct an imported environment, follow imports, load IR or run candidate initializers.
Reject new axiom declarations and direct references to Lean's built-in proof escape hatches.
CI remains responsible for transitive dependencies, downstream importers and global ownership.
Run after the normal kernel-checked build; this does not certify arbitrary .olean files or freshness.
-/

open Lean

namespace LocalAxioms

def forbidden : NameSet := .ofList
  [`sorryAx, `Lean.ofReduceBool, `Lean.ofReduceNat, `Lean.trustCompiler]

/-- Check the main part before touching possibly stale server/private parts. -/
@[noinline] def header (path : System.FilePath) : IO (Bool × CompactedRegion) := do
  let (data, region) ← readModuleData path
  return (data.isModule, region)

/-- Return only owned strings/numbers and region handles. No mmap-backed names escape this frame.
The last part contains the complete private declarations: earlier exported signatures may present
hidden definitions as axioms, so inspecting those instead would produce false positives. -/
@[noinline] def inspect (artifacts : System.FilePath) (mod : Name) :
    IO (Nat × Array String × Array CompactedRegion) := do
  let path := modToFilePath artifacts mod "olean"
  let (isModule, region) ← header path
  unsafe region.free
  unless isModule do
    return (0, #[s!"{mod}: missing leading module declaration"], #[])
  let parts ← readModuleDataParts #[path,
    OLeanLevel.server.adjustFileName path, OLeanLevel.private.adjustFileName path]
  let some (data, _) := parts[2]? | throw <| IO.userError "missing private module data"
  let mut messages : Array String := #[]
  for c in data.constants do
    if c matches .axiomInfo _ then
      messages := messages.push s!"{mod}: {c.name}: axiom declaration"
    for used in c.getUsedConstantsAsSet do
      if forbidden.contains used then
        messages := messages.push s!"{mod}: {c.name} directly uses {used}"
  return (data.constants.size, messages, parts.map (·.2))

def run (modulesFile root : String) (artifacts : System.FilePath) : IO UInt32 := do
  let text ← IO.FS.readFile modulesFile
  let mut modules : NameSet := {}
  for line in text.splitOn "\n" do
    let line := line.trimAscii.toString
    if line.isEmpty then continue
    let mod := line.toName
    unless mod.toString == line && (mod == root.toName || root.toName.isPrefixOf mod) do
      throw <| IO.userError s!"invalid local audit module: {line}"
    modules := modules.insert mod
  -- No search path, sysroot discovery or Lake initialization, even for nonempty scopes.
  let mut declarations := 0
  let mut bad := 0
  for mod in modules do
    let (count, messages, regions) ← inspect artifacts mod
    -- All ModuleData references have died in inspect before we release their regions.
    for region in regions.reverse do unsafe region.free
    declarations := declarations + count
    IO.println s!"local-axioms: {mod}: checked {count} compiled declaration(s)"
    for message in messages do
      IO.eprintln s!"local-axioms: {message}"
      bad := bad + 1
  IO.println s!"local-axioms: checked {modules.size} changed module(s), {declarations} declaration(s); \
    {bad} violation(s). Transitive axiom and global ownership checks run in CI."
  return if bad == 0 then 0 else 1

end LocalAxioms

def main (args : List String) : IO UInt32 := do
  match args with
  | [modulesFile, root, artifacts] =>
    try LocalAxioms.run modulesFile root artifacts
    catch e =>
      IO.eprintln s!"local-axioms: {e}"
      return 2
  | _ =>
    IO.eprintln "usage: LocalAxioms.lean MODULES_FILE ROOT_MODULE ARTIFACT_DIRECTORY"
    return 2
