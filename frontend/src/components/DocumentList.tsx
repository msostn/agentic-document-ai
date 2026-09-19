import type { Document } from '../types'
import { StatusBadge } from './StatusBadge'

interface DocumentListProps {
  documents: Document[]
  activeDocumentId: string | null
  onSelect: (documentId: string) => void
}

export function DocumentList({
  documents,
  activeDocumentId,
  onSelect,
}: DocumentListProps) {
  if (documents.length === 0) {
    return (
      <div className="document-list document-list--empty">
        <p>No documents uploaded yet.</p>
      </div>
    )
  }

  return (
    <div className="document-list">
      <h2>Documents</h2>
      <ul className="document-list__items">
        {documents.map((doc) => {
          const isActive = doc.id === activeDocumentId
          const isReady = doc.status === 'ready'
          return (
            <li
              key={doc.id}
              className={`document-list__item ${isActive ? 'document-list__item--active' : ''} ${!isReady ? 'document-list__item--disabled' : ''}`}
            >
              <button
                type="button"
                className="document-list__button"
                onClick={() => onSelect(doc.id)}
                disabled={!isReady}
                aria-pressed={isActive}
              >
                <span className="document-list__filename">{doc.filename}</span>
                <StatusBadge status={doc.status} />
                {doc.chunk_count != null && (
                  <span className="document-list__chunks">
                    {doc.chunk_count} chunks
                  </span>
                )}
                {doc.error_message && (
                  <span className="document-list__error">
                    {doc.error_message}
                  </span>
                )}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
