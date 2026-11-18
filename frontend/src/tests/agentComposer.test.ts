import { beforeEach, describe, expect, it, vi } from 'vitest'
import { isValidElement, type ReactNode } from 'react'
import { ChatComposer, type StrategyDraft } from '../components/agent/ChatComposer'

const hooks = vi.hoisted(() => ({ values: [] as unknown[], refs: [] as { current: unknown }[], effects: [] as (() => unknown)[], stateIndex: 0, refIndex: 0 }))
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
  useEffect: (effect: () => unknown) => { hooks.effects.push(effect) },
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
function render(onSubmit: (value: string) => Promise<void>, disabled = false, strategyDraft?: StrategyDraft) {
  hooks.stateIndex = 0; hooks.refIndex = 0
  hooks.effects = []
  const tree = ChatComposer({ disabled, loading: false, onSubmit, strategyDraft })
  hooks.effects.forEach(effect => effect())
  return { tree, input: find(tree, 'agent-composer-input'), button: find(tree, 'agent-send-button') }
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

  it('loads each strategy draft once without automatically sending or replacing later edits', () => {
    const submit = vi.fn().mockResolvedValue(undefined)
    const draft = { text: '请研究 MA5 策略', prefillId: 'draft-1' }
    render(submit, false, draft)
    let view = render(submit, false, draft)
    expect(view.input.value).toBe(draft.text)
    expect(submit).not.toHaveBeenCalled()
    ;(view.input.onChange as (event: {target: {value: string}}) => void)({target:{value:'修改后的策略需求'}})
    view = render(submit, false, draft)
    expect(view.input.value).toBe('修改后的策略需求')
    expect(submit).not.toHaveBeenCalled()
  })

  it('preserves an existing input and waits for a choice when a strategy draft arrives', () => {
    const submit = vi.fn().mockResolvedValue(undefined)
    let view = render(submit)
    ;(view.input.onChange as (event: {target: {value: string}}) => void)({target:{value:'尚未发送的原问题'}})
    const draft = {text:'另一个策略',prefillId:'draft-conflict'}
    render(submit, false, draft)
    view = render(submit, false, draft)
    expect(view.input.value).toBe('尚未发送的原问题')
    expect(find(view.tree,'strategy-draft-conflict')).toBeTruthy()
    expect(submit).not.toHaveBeenCalled()
  })
})
