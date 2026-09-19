import { useRef, useState } from 'react'
import type { UploadState } from '../types'
import { uploadDocument } from '../api'
import { ApiError } from '../api'

interface UploadPanelProps {
  uploadState: UploadState
  onUploadStart: () => void
  onUploadSuccess: () => void
  onUploadError: (message: string) => void
  onReset: () => void
}

export function UploadPanel({
  uploadState,
  onUploadStart,
  onUploadSuccess,
  onUploadError,
  onReset,
}: UploadPanelProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    setSelectedFile(file || null)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedFile) return

    onUploadStart()
    try {
      await uploadDocument(selectedFile)
      setSelectedFile(null)
      if (fileRef.current) fileRef.current.value = ''
      onUploadSuccess()
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : 'Upload failed'
      onUploadError(message)
    }
  }

  const isUploading = uploadState === 'uploading'

  return (
    <div className="upload-panel">
      <h2>Upload PDF</h2>
      <form onSubmit={handleSubmit} className="upload-panel__form">
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,application/pdf"
          onChange={handleFileChange}
          disabled={isUploading}
          className="upload-panel__input"
          aria-label="Select PDF file"
        />
        <button
          type="submit"
          disabled={!selectedFile || isUploading}
          className="upload-panel__submit"
        >
          {isUploading ? 'Uploading...' : 'Upload'}
        </button>
      </form>
      <p className="upload-panel__hint">
        PDF files only. Max 25 MB. The original file is not stored after
        ingestion.
      </p>
      {uploadState === 'success' && (
        <p className="upload-panel__success">Document uploaded successfully.</p>
      )}
      {uploadState === 'error' && (
        <button
          type="button"
          className="upload-panel__retry"
          onClick={onReset}
        >
          Dismiss
        </button>
      )}
    </div>
  )
}
