# AI Factory

## Team

The Tablekeeper submission was produced using a three-agent software factory in BAND.

### AI Factory Lead

Harness: OpenCode  
Model: zai-org/GLM-5.3-Flash

Responsibilities:
- Read the current stage specification
- Plan the stage
- Delegate implementation to the Developer
- Coordinate fixes after verification failures
- Produce the final stage-completion report

### AI Factory Developer

Harness: Codex  
Model: gpt-6-astra

Responsibilities:
- Implement stage requirements
- Run implementation tests and checks
- Commit completed work
- Hand the resulting revision and evidence to the Verifier

### AI Factory Verifier

Harness: OpenCode  
Model: zai-org/GLM-5.3-Flash

Responsibilities:
- Independently inspect the implementation
- Run tests and validation
- Check requirements and regressions
- Return failures to the Developer
- Report successful verification to the Lead

## Workflow

The factory used the following handoff sequence:

Human  
→ AI Factory Lead  
→ AI Factory Developer  
→ AI Factory Verifier  
→ AI Factory Lead  
→ Human

When verification failed, work returned through:

AI Factory Verifier  
→ AI Factory Developer  
→ AI Factory Verifier

The Developer supplied implementation evidence and commit revisions to the Verifier.

The Verifier independently tested completed work rather than relying only on the Developer's claims.

The Lead produced the final completion report only after successful verification.

## Stage Progression

The application was developed progressively through:

1. Stage 1
2. Stage 2
3. Stage 3
4. Stage 4

Each stage is preserved in its corresponding `stage-N` directory.

Later stages extend the functionality of earlier stages while preserving previous behavior.

## Final Validation

After Stage 4 completion, the complete repository was tested using the official Tablekeeper harness with all stages in isolated mode.

The final shipped-check result reported:

- `stage-1/` claims Stage 1
- `stage-2/` claims Stage 2
- `stage-3/` claims Stage 3
- `stage-4/` claims Stage 4

The complete BAND room history used to demonstrate factory collaboration is provided in `room.json`.
