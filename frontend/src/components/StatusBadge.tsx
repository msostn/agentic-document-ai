interface StatusBadgeProps {
  status: string
}

const STATUS_LABELS: Record<string, string> = {
  processing: 'Processing',
  ready: 'Ready',
  failed: 'Failed',
  empty: 'No text found',
}

export function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-badge--${status}`}>
      {STATUS_LABELS[status] || status}
    </span>
  )
}
