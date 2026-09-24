export interface SkillSummary {
  name: string
  version: string
  description: string
  source_type: 'builtin' | 'editor' | 'upload' | 'github'
  owner_user_id: string
  dir_path: string
  content_hash: string
  status: 'admitted' | 'disabled' | 'rejected'
  has_scripts: boolean
  rejection_reason: string
  source_url: string
  commit_hash: string
  created_at: string
  file_names: string[]
  apply_to_workers: string[]
}

export interface SkillDetail extends SkillSummary {
  body: string
}

export interface SkillListData {
  skills: SkillSummary[]
  user_id: string
}

export interface SkillCreatePayload {
  name: string
  description: string
  body: string
}
