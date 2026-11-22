const MEGABYTE = 1024 * 1024

/**
 * Проверка размера файла до отправки: без неё большой файл уходит целиком, а отказ
 * приходит только после загрузки. Пределы — те же, что на сервере (решение 0076).
 */
export function tooLargeMessage(file: File, megabytes: number): string | null {
  return file.size > megabytes * MEGABYTE ? `Файл больше ${megabytes} МБ` : null
}
