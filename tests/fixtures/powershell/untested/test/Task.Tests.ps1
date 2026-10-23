BeforeAll {
    . "$PSScriptRoot/../src/Task.ps1"
}

Describe 'Get-TaskLabel' {
    It 'returns the positive label' {
        Get-TaskLabel 1 | Should -BeExactly 'positive'
    }
    It 'returns the negative label' {
        Get-TaskLabel -1 | Should -BeExactly 'negative'
    }
    It 'returns the zero label' {
        Get-TaskLabel 0 | Should -BeExactly 'zero'
    }
}
