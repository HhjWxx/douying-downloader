export interface Item { id: string; title: string; author?: string; sec_uid?: string; cover?: string; created?: number; media_type?: string }
export interface Target { kind: 'url' | 'items' | 'author' | 'mix' | 'collection' | 'likes' | 'folders'; id?: string; ids?: string[]; url?: string }
export interface Options { limit: number; date_from: string | null; date_to: string | null; media_type: string; quality: string; cleanup: boolean }
export interface Settings { path: string; proxy: string; thread: number; cleanup: boolean }
export interface Job { job_id: string; status: string; title?: string; authors?: string[]; target: Target; options: Options; created_at: string; items: string[]; results: Record<string, any>; progress: Record<string, any>; output_dirs: string[]; error: string; attempt: number; listing_complete: boolean }
export interface Desktop {
  api(method: string, route: string, body?: unknown): Promise<any>;
  browser(action: 'current'): Promise<string>;
  browser(action: string, data?: unknown): Promise<void>;
  chooseDirectory(): Promise<string | null>;
  openPath(path: string): Promise<void>;
  preview(path: string): Promise<string>;
  readReport(path: string): Promise<any>;
  importData(): Promise<any>;
  diagnostics(): Promise<Array<{name: string; ok: boolean; detail: string}>>;
  restart(): Promise<void>;
  onEvent(callback: (event: any) => void): () => void;
}
declare global { interface Window { desktop: Desktop } }
