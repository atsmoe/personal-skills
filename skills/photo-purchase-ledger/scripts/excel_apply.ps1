param([Parameter(Mandatory=$true)][string]$RequestPath,
      [Parameter(Mandatory=$true)][string]$ResultPath)
$ErrorActionPreference = 'Stop'
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$plan = $request.plan
$candidate = [IO.Path]::GetFullPath($request.candidate)
if (Test-Path -LiteralPath $ResultPath) { throw 'Result already exists' }
$excel = $null
$book = $null
function Column-Number([string]$text) {
    $number = 0
    foreach ($letter in $text.ToCharArray()) { $number = $number * 26 + [int]$letter - 64 }
    return $number
}
function Apply-Style($range, [bool]$bold, [double]$height) {
    $format = $plan.config.format
    if ($format.font) { $range.Font.Name = [string]$format.font }
    if ($format.body_size_pt) { $range.Font.Size = [double]$format.body_size_pt }
    $range.Font.Color = 0
    $range.Font.Bold = $bold
    $range.HorizontalAlignment = -4108
    $range.VerticalAlignment = -4108
    $range.WrapText = $true
    $range.Interior.Color = 16777215
    $range.RowHeight = $height
    foreach ($edge in @(7,8,9,10,11,12)) {
        $range.Borders.Item($edge).LineStyle = 1
        $range.Borders.Item($edge).Weight = 2
        $range.Borders.Item($edge).Color = 0
    }
}
try {
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.EnableEvents = $false
    $excel.AskToUpdateLinks = $false
    $excel.AutomationSecurity = 3
    # CorruptLoad is xlNormalLoad (0), never request a repair load.
    $book = $excel.Workbooks.Open($candidate, 0, $false, 5, '', '', $true, 1, '', $false, $false, 0, $false, $false, 0)
    if ($book.ReadOnly) { throw 'Candidate opened read-only' }
    $sheet = $book.Worksheets.Item([string]$plan.sheet)
    if ($plan.insert_count -gt 0) {
        $first = [int]$plan.insert_before
        $last = $first + [int]$plan.insert_count - 1
        $sheet.Rows.Item("${first}:${last}").Insert(-4121) | Out-Null
        $columns = $plan.config.columns
        $left = ($columns.PSObject.Properties.Value | ForEach-Object { Column-Number $_ } | Measure-Object -Minimum).Minimum
        $right = ($columns.PSObject.Properties.Value | ForEach-Object { Column-Number $_ } | Measure-Object -Maximum).Maximum
        foreach ($entry in $plan.rows) {
            $row = [int]$entry.row
            $range = $sheet.Range($sheet.Cells.Item($row,$left), $sheet.Cells.Item($row,$right))
            $range.UnMerge()
            $range.ClearContents()
            $height = if ($entry.role -eq 'detail') { $plan.config.format.detail_height_pt } else { $plan.config.format.summary_height_pt }
            if (-not $height) { $height = $sheet.Rows.Item([int]$plan.config.header_row).RowHeight }
            Apply-Style $range ($entry.role -ne 'detail') ([double]$height)
            foreach ($property in $entry.values.PSObject.Properties) {
                $cell = $sheet.Cells.Item($row, (Column-Number $columns.($property.Name)))
                if ($null -eq $property.Value) { continue }
                if ($property.Name -in @('label','quantity','unit_price','amount') -and $entry.role -eq 'detail') {
                    $numericFormat = '0'
                    if ($property.Name -in @('unit_price','amount')) { $numericFormat = '0.00' }
                    elseif ([string]$property.Value -match '\.(\d+)$') { $numericFormat = '0.' + ('0' * $Matches[1].Length) }
                    $cell.NumberFormat = $numericFormat
                    $cell.Value2 = [double]::Parse([string]$property.Value, [Globalization.CultureInfo]::InvariantCulture)
                } elseif ($property.Name -eq 'date') {
                    $cell.NumberFormat = 'm"月"d"日"'
                    $cell.Value2 = ([DateTime]::ParseExact([string]$property.Value, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)).ToOADate()
                } else {
                    # Force literal text, including source strings starting with =, +, - or @.
                    $cell.NumberFormat = '@'
                    $cell.Value2 = [string]$property.Value
                }
            }
            if ($entry.role -ne 'detail') {
                $label = Column-Number $columns.label
                $amount = Column-Number $columns.amount
                if ($amount -gt $label) { $sheet.Range($sheet.Cells.Item($row,$label),$sheet.Cells.Item($row,$amount-1)).Merge() }
            }
        }
        foreach ($formula in $plan.formulas) {
            $cell = $sheet.Range("$($formula.column)$($formula.row)")
            $cell.NumberFormat = '0.00'
            $cell.Formula = [string]$formula.formula
        }
        foreach ($group in $plan.groups) {
            foreach ($photo in $group.photos) {
                $column = Column-Number $columns.photo
                $hostRange = $sheet.Range($sheet.Cells.Item([int]$photo.start_row,$column),$sheet.Cells.Item([int]$photo.end_row,$column))
                if ($photo.end_row -gt $photo.start_row) { $hostRange.Merge() }
                $picture = $sheet.Shapes.AddPicture([string]$photo.path, 0, -1, 0, 0, -1, -1)
                $picture.LockAspectRatio = -1
                $px = if ($null -ne $plan.config.format.padding_x_px) { [double]$plan.config.format.padding_x_px } else { 14 }
                $py = if ($null -ne $plan.config.format.padding_y_px) { [double]$plan.config.format.padding_y_px } else { 8 }
                $scale = [Math]::Min(($hostRange.Width-$px*1.5)/$picture.Width,($hostRange.Height-$py*1.5)/$picture.Height)
                if ($scale -le 0) { throw 'Photo host has no usable area' }
                $picture.Width = $picture.Width*$scale
                $picture.Left = $hostRange.Left+($hostRange.Width-$picture.Width)/2
                $picture.Top = $hostRange.Top+($hostRange.Height-$picture.Height)/2
                $picture.Placement = 1
            }
        }
    }
    $excel.CalculateFullRebuild()
    if ($excel.CalculationState -ne 0) { throw 'Calculation did not finish' }
    $book.Save()
    $book.Close($false)
    $book = $excel.Workbooks.Open($candidate, 0, $true, 5, '', '', $true, 1, '', $false, $false, 0, $false, $false, 0)
    $sheet = $book.Worksheets.Item([string]$plan.sheet)
    $geometry = @()
    foreach ($group in $plan.groups) {
        foreach ($photo in $group.photos) {
            $col = Column-Number $plan.config.columns.photo
            $hostRange = $sheet.Range($sheet.Cells.Item([int]$photo.start_row,$col),$sheet.Cells.Item([int]$photo.end_row,$col))
            $found = @($sheet.Shapes | Where-Object { $_.TopLeftCell.Row -eq $photo.start_row -and $_.TopLeftCell.Column -eq $col })
            if ($found.Count -ne 1) { throw 'Photo host does not contain exactly one picture' }
            $picture = $found[0]
            $dx = [Math]::Abs(2*($picture.Left-$hostRange.Left)+$picture.Width-$hostRange.Width)
            $dy = [Math]::Abs(2*($picture.Top-$hostRange.Top)+$picture.Height-$hostRange.Height)
            $px = if ($null -ne $plan.config.format.padding_x_px) { [double]$plan.config.format.padding_x_px } else { 14 }
            $py = if ($null -ne $plan.config.format.padding_y_px) { [double]$plan.config.format.padding_y_px } else { 8 }
            $inside = $picture.Width -le $hostRange.Width-$px*1.5+0.05 -and $picture.Height -le $hostRange.Height-$py*1.5+0.05
            $geometry += @{row=$photo.start_row;centered=($dx -le 0.05 -and $dy -le 0.05);inside=$inside;host_width_pt=$hostRange.Width;host_height_pt=$hostRange.Height}
            if (-not $inside -or $dx -gt 0.05 -or $dy -gt 0.05) { throw 'Saved photo geometry failed' }
        }
    }
    $start = [Math]::Max([int]$plan.config.header_row, [int]$plan.insert_before-1)
    $end = [Math]::Max($start+1, [int]$plan.total_row)
    $sheet.PageSetup.PrintArea = "$($plan.config.columns.label)$start`:$($plan.config.columns.photo)$end"
    $sheet.PageSetup.Zoom = $false
    $sheet.PageSetup.FitToPagesWide = 1
    $sheet.PageSetup.FitToPagesTall = $false
    $sheet.ExportAsFixedFormat(0,[string]$request.preview)
    $book.Close($false)
    $book = $null
    $result = @{schema='purchase-ledger-excel-result/v1';normal_open=$true;recalculated=$true;
                candidate_sha256=(Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLower();preview=$request.preview;geometry=$geometry}
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding utf8NoBOM
} catch {
    throw "Native Excel stage failed at $($_.ScriptStackTrace): $($_.Exception.Message)"
} finally {
    if ($book) { $book.Close($false) }
    if ($excel) { $excel.Quit(); [Runtime.InteropServices.Marshal]::FinalReleaseComObject($excel) | Out-Null }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
