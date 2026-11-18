import { beforeEach, describe, expect, it, vi } from 'vitest'
import { isValidElement, type ReactNode } from 'react'
import { ChatComposer } from '../components/agent/ChatComposer'

const hooks = vi.hoisted(() => ({ values: [] as unknown[], refs: [] as { current: unknown }[], stateIndex: 0, refIndex: 0 }))
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => {
    const index = hooks.stateIndex++
    if (!(index in hooks.values)) hooks.values[index] = initial
    return [hooks.values[index], (value: unknown) => { hooks.values[index] = typeof value === 'function' ? value(hooks.values[index]) : value }]
  },
  useRef: (initial: unknown) => {
    const index = hooks.refIndex++
    hooks.refs[index] ??= { current: initial }
    return hooks.refs[index]
  },
}))
vi.mock('antd', () => ({ Button: 'button', Input: { TextArea: 'textarea' }, Space: 'div', Typography: { Text: 'span' } }))

type Props = Record<string, unknown> & { children?: ReactNode }
function find(node: ReactNode, testId: string): Props {
  if (isValidElement<Props>(node)) {
    if (node.props['data-testid'] === testId) return node.props
    for (const child of [node.props.children].flat(Infinity) as ReactNode[]) {
      try { return find(child, testId) } catch { /* Try the next rendered child. */ }
    }
  }
  throw new Error(`Missing ${testId}`)
}
function render(onSubmit: (value: string) => Promise<void>, disabled = false) {
  hooks.stateIndex = 0; hooks.refIndex = 0
  const tree = ChatComposer({ disabled, loading: false, onSubmit })
  return { input: find(tree, 'agent-composer-input'), button: find(tree, 'agent-send-button') }
}

beforeEach(() => { hooks.values = []; hooks.refs = [] })

describe('Agent composer submission recovery', () => {
  it('preserves the draft after a rejected request and allows a successful retry', async () => {
    const submit = vi.fn().mockRejectedValueOnce(new Error('HTTP 422')).mockResolvedValueOnce(undefined)
    let view = render(submit)
    ;(view.input.onChange as (event: { target: { value: string } }) => void)({ target: { value: '查看模拟盘' } })
    view = render(submit)
    ;(view.button.onClick as () => void)()
    await vi.waitFor(() => expect(render(submit).button.loading).toBe(false))
    view = render(submit)
    expect(view.input.value).toBe('查看模拟盘')
    expect(view.button.disabled).toBe(false)
    ;(view.button.onClick as () => void)()
    await vi.waitFor(() => expect(render(submit).input.value).toBe(''))
    expect(submit.mock.calls).toEqual([['查看模拟盘'], ['查看模拟盘']])
  })

  it('prevents repeated Enter/click submission before React updates pending props', async () => {
    let finish!: () => void
    const submit = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    let view = render(submit)
    ;(view.input.onChange as (event: { target: { value: string } }) => void)({ target: { value: '分析持仓' } })
    view = render(submit)
    ;(view.button.onClick as () => void)()
    ;(view.button.onClick as () => void)()
    ;(view.input.onPressEnter as (event: { shiftKey: boolean; preventDefault: () => void }) => void)({ shiftKey: false, preventDefault: vi.fn() })
    expect(submit).toHaveBeenCalledOnce()
    expect(render(submit).button.loading).toBe(true)
    finish()
    await vi.waitFor(() => expect(render(submit).button.loading).toBe(false))
  })
})
