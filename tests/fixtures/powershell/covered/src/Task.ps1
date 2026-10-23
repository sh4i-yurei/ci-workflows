function Get-TaskLabel {
    param([int]$Value)
    if ($Value -gt 0) {
        return 'positive'
    }
    if ($Value -lt 0) {
        return 'negative'
    }
    return 'zero'
}

function Get-Witness {
    return 'covered'
}
