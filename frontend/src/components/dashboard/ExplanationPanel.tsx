import { Card, Empty, Typography } from 'antd'
import type { StockExplanation } from '../../types/stocks'
export function ExplanationPanel({ explanation }: { explanation?: StockExplanation }) { return <Card title="已有 AI 解释缓存">{explanation?.available?<Typography.Paragraph style={{whiteSpace:'pre-wrap'}}>{typeof explanation.cached==='string'?explanation.cached:JSON.stringify(explanation.cached,null,2)}</Typography.Paragraph>:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无解释缓存"/>}</Card> }
