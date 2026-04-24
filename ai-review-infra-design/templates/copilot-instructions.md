# Copilot Instructions

When reviewing pull requests:

1. Focus on high-impact problems only:
   - security
   - authorization
   - data loss
   - race conditions
   - breaking API changes
   - missing tests for risky behavior

2. Ignore formatting and style issues handled by CI.

3. Do not leave comments for low-confidence guesses.

4. Prefer concrete, actionable findings with file and line references.

5. Treat review output as advisory, not a replacement for tests or static analysis.
