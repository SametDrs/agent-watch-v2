import { describe, expect, mock, test } from 'claude-code/testing'
import type { CommandRunInput } from 'claude-code'

const RUN_AGENT_WATCH: CommandRunInput = {
  command: 'agent-watch',
  args: '',
  origin: { kind: 'composer' },
  presentation: { isFullscreen: false, columns: 120 },
}

const OK_OPENED = {
  exitCode: 0,
  stdout: 'Agent Watch opened\n',
  stderr: '',
  isStdoutTruncated: false,
  isStderrTruncated: false,
} as const

describe('agent-watch mod', () => {
  test('session.start registers agent-watch and runs launcher with --new-window --auto', async ($, on) => {
    const clock = mock.clock(on)
    const registered: Array<{ name: string; description: string }> = []
    const runs: Array<{ argv: string[]; timeoutMs?: number }> = []

    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('command.register', (_, e) => {
      registered.push({ name: e.name, description: e.description })
      return { value: { command: e.name } }
    })
    on('process.run', (_, e) => {
      runs.push({ argv: [...e.argv], timeoutMs: e.init?.timeoutMs })
      return { value: { ...OK_OPENED } }
    })

    const result = await $.session.start({
      cwd: '/',
      surface: 'terminal',
      isInteractive: true,
    })
    await clock.settle()

    expect(result).toEqual({ cwd: '/' })
    expect(registered).toEqual([
      { name: 'agent-watch', description: 'Open the Agent Watch window' },
    ])
    expect(runs).toEqual([
      { argv: ['agent-watch', '--new-window', '--auto'], timeoutMs: 15000 },
    ])
    expect(runs[0]?.argv.slice(-2)).toEqual(['--new-window', '--auto'])
  })

  test(
    'uses configured exe option for session.start and /agent-watch',
    { options: { exe: 'C:\\custom\\agent-watch.exe' } },
    async ($, on) => {
      const clock = mock.clock(on)
      const argvs: string[][] = []

      on('session.start', (_, e) => ({ cwd: e.cwd }))
      on('command.register', (_, e) => ({ value: { command: e.name } }))
      on('process.run', (_, e) => {
        argvs.push([...e.argv])
        return { value: { ...OK_OPENED } }
      })

      await $.session.start({
        cwd: '/',
        surface: 'terminal',
        isInteractive: true,
      })
      await clock.settle()

      const reply = await $.command.run(RUN_AGENT_WATCH)
      expect(reply).toEqual({ text: 'Agent Watch opened' })
      expect(argvs).toEqual([
        ['C:\\custom\\agent-watch.exe', '--new-window', '--auto'],
        ['C:\\custom\\agent-watch.exe', '--new-window'],
      ])
    },
  )

  test('/agent-watch returns text from stubbed run and makes no model call', async ($, on) => {
    const clock = mock.clock(on)
    const runs: string[][] = []
    const modelCalls: string[] = []
    let nextCalled = false

    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('command.register', (_, e) => ({ value: { command: e.name } }))
    on('process.run', (_, e) => {
      runs.push([...e.argv])
      return {
        value: {
          exitCode: 0,
          stdout: 'Agent Watch already open\r\n',
          stderr: '',
          isStdoutTruncated: false,
          isStderrTruncated: false,
        },
      }
    })
    on('command.run', () => {
      nextCalled = true
      return { text: 'unexpected next call' }
    })
    on('prompt.submit', () => {
      modelCalls.push('prompt.submit')
      return { drop: 'unexpected' }
    })
    on('model.complete', () => {
      modelCalls.push('model.complete')
      return { deny: 'unexpected' }
    })
    on('model.classify', () => {
      modelCalls.push('model.classify')
      return { deny: 'unexpected' }
    })
    on('model.fork', () => {
      modelCalls.push('model.fork')
      return { deny: 'unexpected' }
    })

    await $.session.start({
      cwd: '/',
      surface: 'terminal',
      isInteractive: true,
    })
    await clock.settle()
    runs.length = 0

    const reply = await $.command.run(RUN_AGENT_WATCH)

    expect(reply).toEqual({ text: 'Agent Watch already open' })
    expect(runs).toEqual([['agent-watch', '--new-window']])
    expect(nextCalled).toBe(false)
    expect(modelCalls).toEqual([])
  })

  test('session.start surfaces spawn rejection via toast and debug log without throwing', async ($, on) => {
    const clock = mock.clock(on)
    const toasts: string[] = []
    const logs: Array<{ text: string; to: string }> = []

    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('command.register', (_, e) => ({ value: { command: e.name } }))
    on('process.run', () => ({ deny: 'spawn ENOENT' }))
    on('ui.toast', (_, e) => {
      toasts.push(e.text)
      return { value: undefined }
    })
    on('ui.log', (_, e) => {
      logs.push({ text: e.text, to: e.to })
      return { value: undefined }
    })

    await expect(
      $.session.start({
        cwd: '/',
        surface: 'terminal',
        isInteractive: true,
      }),
    ).resolves.toEqual({ cwd: '/' })
    await clock.settle()

    expect(toasts).toHaveLength(1)
    expect(toasts[0]).toMatch(/^Agent Watch failed to open: .*spawn ENOENT/)
    expect(logs).toEqual([
      {
        text: expect.stringMatching(/^Agent Watch failed to open: .*spawn ENOENT/),
        to: 'debug',
      },
    ])
  })

  test('session.start surfaces non-zero exit code via toast and debug log without throwing', async ($, on) => {
    const clock = mock.clock(on)
    const toasts: string[] = []
    const logs: Array<{ text: string; to: string }> = []

    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('command.register', (_, e) => ({ value: { command: e.name } }))
    on('process.run', () => ({
      value: {
        exitCode: 1,
        stdout: 'Agent Watch failed to open: launch error\n',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }))
    on('ui.toast', (_, e) => {
      toasts.push(e.text)
      return { value: undefined }
    })
    on('ui.log', (_, e) => {
      logs.push({ text: e.text, to: e.to })
      return { value: undefined }
    })

    await expect(
      $.session.start({
        cwd: '/',
        surface: 'terminal',
        isInteractive: true,
      }),
    ).resolves.toEqual({ cwd: '/' })
    await clock.settle()

    expect(toasts).toEqual(['Agent Watch failed to open: launch error'])
    expect(logs).toEqual([
      { text: 'Agent Watch failed to open: launch error', to: 'debug' },
    ])
  })

  test('/agent-watch surfaces spawn errors as command text without model call', async ($, on) => {
    const clock = mock.clock(on)
    let mode: 'reject' | 'exit1' = 'reject'

    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('command.register', (_, e) => ({ value: { command: e.name } }))
    on('ui.toast', () => ({ value: undefined }))
    on('ui.log', () => ({ value: undefined }))
    on('process.run', () => {
      if (mode === 'reject') return { deny: 'spawn ENOENT' }
      return {
        value: {
          exitCode: 1,
          stdout: '',
          stderr: 'executable failed\n',
          isStdoutTruncated: false,
          isStderrTruncated: false,
        },
      }
    })

    await $.session.start({
      cwd: '/',
      surface: 'terminal',
      isInteractive: true,
    })
    await clock.settle()

    const rejected = await $.command.run(RUN_AGENT_WATCH)
    expect(rejected.text).toMatch(/^Agent Watch failed to open: .*spawn ENOENT/)

    mode = 'exit1'
    const failed = await $.command.run(RUN_AGENT_WATCH)
    expect(failed).toEqual({ text: 'Agent Watch failed to open: executable failed' })
  })
})
