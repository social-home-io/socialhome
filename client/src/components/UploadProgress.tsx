/**
 * UploadProgress — file upload indicator (§23.73).
 */
import { signal } from '@preact/signals'

export const uploadProgress = signal<{ filename: string; percent: number } | null>(null)

export interface UploadResult {
  /** Canonical (unsigned) URL — store this on the post / message
   *  ``media_url`` field. The server signs fresh on every read. */
  url: string
  /** Signed URL the SPA can drop straight into ``<img src>`` /
   *  ``<video src>`` for the immediate post-upload preview. */
  signed_url: string
  filename: string
}

export async function uploadWithProgress(file: File): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const formData = new FormData()
    formData.append('file', file)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        uploadProgress.value = { filename: file.name, percent: Math.round((e.loaded / e.total) * 100) }
      }
    }
    xhr.onload = () => {
      uploadProgress.value = null
      if (xhr.status < 300) {
        const data = JSON.parse(xhr.responseText)
        // Server returns ``{url, signed_url, filename}``. Pre-signed
        // URL backend rollouts may omit ``signed_url`` — fall back to
        // the canonical URL so the preview at least attempts to load.
        resolve({
          url: data.url || data.filename,
          signed_url: data.signed_url || data.url || data.filename,
          filename: data.filename,
        })
      } else {
        reject(new Error(`Upload failed: ${xhr.status}`))
      }
    }
    xhr.onerror = () => { uploadProgress.value = null; reject(new Error('Upload failed')) }
    xhr.open('POST', '/api/media/upload')
    const token = localStorage.getItem('sh_token')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.send(formData)
  })
}

export function UploadProgressBar() {
  const p = uploadProgress.value
  if (!p) return null
  return (
    <div class="sh-upload-progress" role="progressbar" aria-valuenow={p.percent} aria-valuemin={0} aria-valuemax={100}>
      <span class="sh-upload-filename">{p.filename}</span>
      <div class="sh-upload-bar"><div class="sh-upload-fill" style={{ width: `${p.percent}%` }} /></div>
      <span class="sh-upload-pct">{p.percent}%</span>
    </div>
  )
}
