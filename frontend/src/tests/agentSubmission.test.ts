import { afterEach, describe, expect, it } from 'vitest'
import { AxiosError, type AxiosResponse } from 'axios'
import fixture from './fixtures/agent_task_request.json'
import { agentApi, agentRunRequest } from '../api/agentApi'
import { submitTask } from '../api/taskApi'
import { httpClient } from '../api/httpClient'
import { ApiContractError, errorMessage } from '../api/errors'

const adapter = httpClient.defaults.adapter
afterEach(() => { httpClient.defaults.adapter = adapter })

it('sends the same Agent task payload validated by the real FastAPI route', async () => {
  const requests: Array<{ url?: string; body: unknown }> = []
  httpClient.defaults.adapter = async (config) => {
    requests.push({ url: config.url, body: JSON.parse(config.data) })
    return { config, status: 200, statusText: 'OK', headers: {}, data: { success: true, data: { task_id: 'accepted' } } }
  }
  const question = fixture.kwargs.query
  await agentApi.createMessage(fixture.owner_id, fixture.session_id, question, fixture.metadata.user_message_id)
  await submitTask(agentRunRequest(fixture.owner_id, fixture.session_id, question, fixture.metadata.user_message_id))

  expect(requests[0]).toMatchObject({ url: `/api/v1/web/agent/sessions/${fixture.session_id}/messages`, body: { content: question, message_id: fixture.metadata.user_message_id } })
  expect(requests[1]).toEqual({ url: '/api/v1/tasks', body: fixture })
})

function responseError(status: number, data: unknown) {
  return new AxiosError('Request failed with status code ' + status, undefined, undefined, undefined, { status, data } as AxiosResponse)
}

describe('readable API errors', () => {
  it('explains the rejected legacy Agent contract', () => {
    expect(errorMessage(responseError(422, { detail: 'task_positional_arguments_not_allowed' }))).toBe('发送请求格式已失效，请刷新页面后重试。')
  })
  it('shows server envelope messages and string validation details', () => {
    expect(errorMessage(responseError(400, { error: { message: '因子表达式不合法' } }))).toBe('因子表达式不合法')
    expect(errorMessage(responseError(422, { detail: '因子名称已存在' }))).toBe('因子名称已存在')
    expect(errorMessage(new ApiContractError({ code: 'INVALID', message: '配置不完整' }))).toBe('配置不完整')
  })
  it('does not echo submitted input or context from FastAPI validation errors', () => {
    const result = errorMessage(responseError(422, { detail: [{ loc: ['body', 'content'], msg: 'String should have at most 20000 characters', input: 'private user message', ctx: { credential: 'private-key' } }] }))
    expect(result).toContain('content')
    expect(result).toContain('20000')
    expect(result).not.toContain('private')
  })
  it('handles server and network failures without dumping the response or request', () => {
    expect(errorMessage(responseError(503, { trace: 'private trace' }))).toBe('服务暂时不可用，请稍后重试。')
    expect(errorMessage(new AxiosError('Network Error'))).toBe('网络连接失败，请检查网络后重试。')
  })
})
