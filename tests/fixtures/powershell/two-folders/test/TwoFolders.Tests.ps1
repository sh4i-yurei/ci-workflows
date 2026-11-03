BeforeAll {
    . "$PSScriptRoot/../first/First.ps1"
    . "$PSScriptRoot/../second/Second.ps1"
}

Describe 'two source folders' {
    It 'covers the first folder' {
        Get-FirstLabel | Should -BeExactly 'first'
    }
    It 'covers the second folder' {
        Get-SecondLabel | Should -BeExactly 'second'
    }
}
