import type { ProcessRunResult, Register } from 'claude-code'

const DEFAULT_EXE = 'agent-watch'
const AUTO_OPEN_TIMEOUT_MS = 15_000

function resolveExe(raw: unknown): string {
  return typeof raw === 'string' && raw.trim().length > 0
    ? raw.trim()
    : DEFAULT_EXE
}

function firstLine(text: string): string {
  return text.trim().split(/\r?\n/)[0]?.trim() ?? ''
}

function formatSpawnFailure(detail: string): string {
  const clean = detail.replace(/\s+/g, ' ').trim()
  if (clean.startsWith('Agent Watch')) return clean
  return clean.length > 0
    ? `Agent Watch failed to open: ${clean}`
    : 'Agent Watch failed to open'
}

function failureFromResult(result: ProcessRunResult): string {
  const line = firstLine(result.stdout) || firstLine(result.stderr)
  return formatSpawnFailure(line || `exit code ${result.exitCode}`)
}

function failureFromError(error: unknown): string {
  const msg = error instanceof Error ? error.message : String(error)
  return formatSpawnFailure(msg)
}

export const register: Register = (on, options) => {
  const exe = resolveExe(options.exe)

  on('session.start', async ($, e, next) => {
    const started = await next(e)

    await $.command.register({
      name: 'agent-watch',
      description: 'Open the Agent Watch window',
    })

    const reportError = (message: string): void => {
      try {
        $.ui.log(message, { to: 'debug' })
      } catch {
        // Never allow debug logging failures to escape session.start.
      }
      try {
        $.ui.toast(message)
      } catch {
        // Never allow toast failures to escape session.start.
      }
    }

    void (async () => {
      try {
        const result = await $.process.run(
          [exe, '--new-window', '--auto'],
          { timeoutMs: AUTO_OPEN_TIMEOUT_MS },
        )
        if (result.exitCode !== 0) {
          reportError(failureFromResult(result))
        }
      } catch (error) {
        reportError(failureFromError(error))
      }
    })()

    return started
  })

  on('command.run', { command: 'agent-watch' }, async $ => {
    try {
      const result = await $.process.run([exe, '--new-window'])
      if (result.exitCode === 0) {
        const line = firstLine(result.stdout)
        return { text: line || 'Agent Watch opened' }
      }
      return { text: failureFromResult(result) }
    } catch (error) {
      return { text: failureFromError(error) }
    }
  })
}
