import { useState, useEffect, useCallback } from 'react'
import type { Document, UploadState } from './types'
import { listDocuments, ApiError } from './api'
import { UploadPanel } from './components/UploadPanel'
import { DocumentList } from './components/DocumentList'
import { ChatPanel } from './components/ChatPanel'
import { ErrorBanner } from './components/ErrorBanner'
import './App.css'

function App() {
  const [documents, setDocuments] = useState<Document[]>([])
  const [activeDocumentId, setActiveDocumentId] = useState<string | null>(null)
  const [uploadState, setUploadState] = useState<UploadState>('idle')
  const [fetchError, setFetchError] = useState<string | null>(null)

  const fetchDocuments = useCallback(async () => {
    try {
      const docs = await listDocuments()
      setDocuments(docs)
      setFetchError(null)
    } catch (err) {
      const message =
        err instanceof ApiError
          ? 'Cannot reach the backend server.'
          : 'Failed to load documents.'
      setFetchError(message)
    }
  }, [])

  useEffect(() => {
    fetchDocuments()
  }, [fetchDocuments])

  const handleUploadStart = () => {
    setUploadState('uploading')
    setFetchError(null)
  }

  const handleUploadSuccess = async () => {
    setUploadState('success')
    await fetchDocuments()
  }

  const handleUploadError = (message: string) => {
    setUploadState('error')
    setFetchError(message)
  }

  const handleUploadReset = () => {
    setUploadState('idle')
    setFetchError(null)
  }

  const handleSelectDocument = (documentId: string) => {
    setActiveDocumentId(documentId)
  }

  const activeDocument = documents.find((d) => d.id === activeDocumentId)
  const isDocumentReady = activeDocument?.status === 'ready'

  return (
    <div className="app">
      <header className="app__header">
        <h1>Agentic Document Intelligence</h1>
        <p>Upload a PDF and ask questions grounded in that document only.</p>
      </header>

      {fetchError && uploadState !== 'error' && (
        <ErrorBanner message={fetchError} onDismiss={() => setFetchError(null)} />
      )}

      <div className="app__layout">
        <aside className="app__sidebar">
          <UploadPanel
            uploadState={uploadState}
            onUploadStart={handleUploadStart}
            onUploadSuccess={handleUploadSuccess}
            onUploadError={handleUploadError}
            onReset={handleUploadReset}
          />
          <DocumentList
            documents={documents}
            activeDocumentId={activeDocumentId}
            onSelect={handleSelectDocument}
          />
        </aside>
        <main className="app__main">
          <ChatPanel
            documentId={activeDocumentId}
            documentReady={!!isDocumentReady}
          />
        </main>
      </div>
    </div>
  )
}

export default App
