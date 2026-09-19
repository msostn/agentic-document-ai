import type { AnswerSource } from '../types'

interface SourcesListProps {
  sources: AnswerSource[]
}

export function SourcesList({ sources }: SourcesListProps) {
  if (sources.length === 0) return null

  return (
    <div className="sources-list">
      <h4 className="sources-list__title">Sources</h4>
      <ul className="sources-list__items">
        {sources.map((source, idx) => (
          <li key={`${source.chunk_id}-${idx}`} className="sources-list__item">
            <span className="sources-list__page">
              Page {source.page_number}
            </span>
            <span className="sources-list__chunk">
              Chunk {source.chunk_index}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
