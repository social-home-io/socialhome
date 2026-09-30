import { describe, it, expect } from 'vitest'
import {
  isPeriodsEligible, periodRows, timelineGeometry, blockBox, lessonNumber, dayEntries,
  BASE_PPM, COMPRESSED_PX, MIN_BLOCK_PX, nextStartFor, prefillAt,
} from './layout'
import { entry, timetable, schoolWeek } from './testUtils'

describe('Periods eligibility', () => {
  it('is eligible when every day has the same slot sequence', () => {
    expect(isPeriodsEligible(timetable({ entries: schoolWeek() }), [0, 1, 2, 3, 4])).toBe(true)
  })

  it('is not eligible when a day differs or is empty', () => {
    const entries = schoolWeek()
    const tuesdayEarly = entries.map(e =>
      e.weekday === 1 && e.start === '08:00' ? { ...e, start: '07:15' } : e)
    expect(isPeriodsEligible(timetable({ entries: tuesdayEarly }), [0, 1, 2, 3, 4])).toBe(false)
    const noFriday = entries.filter(e => e.weekday !== 4)
    expect(isPeriodsEligible(timetable({ entries: noFriday }), [0, 1, 2, 3, 4])).toBe(false)
    expect(isPeriodsEligible(timetable(), [0, 1, 2, 3, 4])).toBe(false)
  })
})

describe('periodRows', () => {
  it('turns a break every day shares into a spanning band', () => {
    const rows = periodRows(timetable({ entries: schoolWeek() }), [0, 1, 2, 3, 4])
    expect(rows.map(r => r.label)).toEqual(['1.', '2.', '', '3.', '4.'])
    const band = rows[2]
    expect(band.band).toBe(true)
    expect(band.title).toBe('Pause')
  })

  it('merges consecutive identical lessons into one rowSpan cell', () => {
    const entries = schoolWeek([0, 1], [['Mathe', 'Mathe', 'Deutsch', 'Deutsch'], ['Mathe', 'Kunst']])
    const rows = periodRows(timetable({ entries, days: [0, 1] }), [0, 1])
    const mon1 = rows[0].cells[0]
    expect(mon1 && mon1 !== 'merged' && mon1.rowSpan).toBe(2)
    expect(rows[1].cells[0]).toBe('merged')
    // Across the break band nothing merges: Deutsch 3.+4. is its own pair.
    const mon3 = rows[3].cells[0]
    expect(mon3 && mon3 !== 'merged' && mon3.rowSpan).toBe(2)
    // Tuesday differs → no merge.
    const tue1 = rows[0].cells[1]
    expect(tue1 && tue1 !== 'merged' && tue1.rowSpan).toBe(1)
  })

  it('merges a triple lesson, but not across a gap longer than gap_minutes', () => {
    const entries = [
      entry(0, '08:00', '08:45', { title: 'Sport' }), entry(0, '08:50', '09:35', { title: 'Sport' }),
      entry(0, '09:40', '10:25', { title: 'Sport' }),
      entry(0, '10:40', '11:25', { title: 'Sport' }), // 15 min later
    ]
    const rows = periodRows(timetable({ entries, days: [0] }), [0])
    const first = rows[0].cells[0]
    expect(first && first !== 'merged' && [first.rowSpan, first.end]).toEqual([3, '10:25'])
    const last = rows[3].cells[0]
    expect(last && last !== 'merged' && last.rowSpan).toBe(1)
  })

  it('never merges untitled slots or lessons in different rooms', () => {
    const entries = [
      entry(0, '08:00', '08:45'), entry(0, '08:50', '09:35'),
      entry(0, '09:40', '10:25', { title: 'Sport', room: 'A' }),
      entry(0, '10:30', '11:15', { title: 'Sport', room: 'B' }),
    ]
    const rows = periodRows(timetable({ entries, days: [0] }), [0])
    expect(rows.every(r => r.cells[0] !== 'merged')).toBe(true)
  })

  it('leaves an empty cell where a day lacks a slot (forced Periods)', () => {
    const entries = [entry(0, '08:00', '08:45'), entry(1, '07:15', '08:00')]
    const rows = periodRows(timetable({ entries, days: [0, 1] }), [0, 1])
    expect(rows.map(r => r.start)).toEqual(['07:15', '08:00'])
    expect(rows[0].cells[0]).toBeNull()
    expect(rows[1].cells[1]).toBeNull()
  })
})

describe('timelineGeometry', () => {
  it('pads the axis by 15 min rounded to the half hour, at the base scale', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '08:45'), entry(0, '08:50', '12:10')] })
    const geo = timelineGeometry(tt, [0])
    expect(geo.axisStart).toBe(7 * 60 + 30)
    expect(geo.axisEnd).toBe(12 * 60 + 30)
    expect(geo.ppm).toBe(BASE_PPM)
    const box = blockBox(geo, tt.entries[0])
    expect(box.top).toBeCloseTo(30 * BASE_PPM)
    expect(box.height).toBeCloseTo(45 * BASE_PPM)
    expect(geo.height).toBeCloseTo(5 * 60 * BASE_PPM)
  })

  it('raises the scale for short lessons, capped at 2 px/min, with a 36 px floor', () => {
    const twenty = timetable({ entries: [entry(0, '08:00', '08:20')] })
    expect(timelineGeometry(twenty, [0]).ppm).toBeCloseTo(36 / 20)
    const five = timetable({ entries: [entry(0, '08:00', '08:05')] })
    const geo = timelineGeometry(five, [0])
    expect(geo.ppm).toBe(2)
    expect(blockBox(geo, five.entries[0]).height).toBe(MIN_BLOCK_PX)
  })

  it('lines days up on a shared axis (an early Tuesday lesson sits higher)', () => {
    const mon = entry(0, '08:00', '08:45')
    const tue = entry(1, '07:15', '08:00', { title: 'Musik' })
    const geo = timelineGeometry(timetable({ entries: [mon, tue] }), [0, 1])
    expect(geo.axisStart).toBe(7 * 60)
    expect(blockBox(geo, tue).top).toBeLessThan(blockBox(geo, mon).top)
    expect(blockBox(geo, mon).top - blockBox(geo, tue).top).toBeCloseTo(45 * geo.ppm)
  })

  it('compresses a > 60 min gap that is empty on every day into a 16 px band', () => {
    const a = entry(0, '08:00', '09:00')
    const b = entry(1, '15:00', '16:00')
    const geo = timelineGeometry(timetable({ entries: [a, b] }), [0, 1])
    const compressed = geo.segments.filter(s => s.compressed)
    expect(compressed).toEqual([{ from: 9 * 60 + 30, to: 14 * 60 + 30, compressed: true }])
    // 07:30–09:30 at full scale, the band, then 14:30–15:00.
    expect(geo.y(15 * 60)).toBeCloseTo(120 * geo.ppm + COMPRESSED_PX + 30 * geo.ppm)
    expect(geo.ticks.some(t => t.minute === 12 * 60)).toBe(false)
    // A click in the band maps to its start; a normal y round-trips.
    expect(geo.minuteAt(120 * geo.ppm + 5)).toBe(9 * 60 + 30)
    expect(geo.minuteAt(geo.y(15 * 60 + 20))).toBeCloseTo(15 * 60 + 20)
  })

  it('does not compress a gap that another day fills', () => {
    const geo = timelineGeometry(timetable({ entries: [
      entry(0, '08:00', '09:00'), entry(0, '15:00', '16:00'), entry(1, '10:00', '14:00'),
    ] }), [0, 1])
    expect(geo.segments.some(s => s.compressed)).toBe(false)
  })

  it('emits hour ticks as major and half hours as minor', () => {
    const geo = timelineGeometry(timetable({ entries: [entry(0, '08:00', '09:00')] }), [0])
    expect(geo.ticks.map(t => [t.minute, t.major])).toEqual([
      [450, false], [480, true], [510, false], [540, true], [570, false],
    ])
  })
})

describe('lesson numbering', () => {
  it('counts lessons only, in time order', () => {
    const tt = timetable({ entries: schoolWeek([1]) })
    const tue = dayEntries(tt, 1)
    expect(tue.map(e => lessonNumber(tt, e))).toEqual([1, 2, null, 3, 4])
  })
})

describe('add-entry prefill', () => {
  it('starts after the last entry plus the gap, or at day_start', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '08:45'), entry(0, '08:50', '09:35')] })
    expect(nextStartFor(tt, 0)).toBe('09:40')
    expect(nextStartFor(tt, 1)).toBe('08:00')
  })

  it('lasts defaults.lesson_minutes and never crosses midnight', () => {
    const tt = timetable()
    expect(prefillAt(tt, 2, 10 * 60)).toEqual({ weekday: 2, start: '10:00', end: '10:45' })
    expect(prefillAt(tt, 2, 23 * 60 + 40)).toEqual({ weekday: 2, start: '23:14', end: '23:59' })
  })
})
