export interface AgentSession {
  conversation_id: string
  user_id: string
  title: string
  status: string
  language: string
  created_at: string
  updated_at: string
  last_message_at: string
  last_run_id: string
}

export interface AgentSessionPage {
  records: AgentSession[]
  total: number
  offset: number
  limit: number
}

export interface AgentMessage {
  message_id: string
  conversation_id: string
  user_id: string
  role: 'user' | 'assistant' | 'system' | string
  content: string
  language: string
  created_at: string
  run_id: string
  task_id: string
  result_summary: Record<string, unknown>
}

export interface AgentMessagePage {
  records: AgentMessage[]
  total: number
  offset: number
  limit: number
}

export interface AgentRuntimeCall {
  tool_call_id: string
  step_id: string
  tool_name: string
  status: string
  call_kind: 'tool' | 'worker' | string
  record_source: string
  error_type: string
  error_message: string
  started_at: string
  finished_at: string
  duration_seconds: number
  retry_count: number
  input_summary: unknown
  output_summary: unknown
}

export interface AgentRunDetail {
  run: Record<string, unknown>
  steps: Array<Record<string, unknown>>
  tool_calls: AgentRuntimeCall[]
  sources: Array<Record<string, unknown>>
  proposals: Array<Record<string, unknown>>
  counts: {
    steps: number
    tool_calls: number
    persisted_tool_calls: number
    worker_calls: number
    sources: number
    proposals: number
  }
}

export interface AgentPendingAction {
  proposal_id: string
  run_id: string
  intent: string
  operation_type: string
  confirmation_status: string
  execution_status: string
  created_at: string
  expires_at: string
  before_state_summary: unknown
  proposed_changes: unknown
  after_state_preview: unknown
  warnings: unknown
  validation_results: unknown
  confirmation_phrase: string
}

export interface AgentPendingActionPage {
  records: AgentPendingAction[]
  total: number
}

export interface AgentStrategyProposal {
  available: boolean
  proposal: Record<string, unknown> | null
  versions: Array<Record<string, unknown>>
}

